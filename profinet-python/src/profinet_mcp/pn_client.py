"""PROFINET client facade: safety switches, timing, and routing to the configured backend."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import anyio

from .backends import (
    Backend,
    BackendNotConfigured,
    BackendUnavailable,
    ClientDisconnected,
    DeviceRejected,
    OperationNotImplemented,
    OperationRefused,
    OutcomeUnknown,
    ProfinetClientError,
    RequestNotSent,
    call_not_after,
    create_backend,
)
from .config import ProfinetConfig

__all__ = [
    "BackendNotConfigured",
    "BackendUnavailable",
    "ClientDisconnected",
    "config_cmds_refusal",
    "DeviceRejected",
    "OperationNotImplemented",
    "OperationRefused",
    "OutcomeUnknown",
    "ProfinetClient",
    "ProfinetClientError",
    "RequestNotSent",
    "match_device",
]

T = TypeVar("T")

WRITES_DISABLED = (
    "Writes are disabled, so nothing was sent. The server is read-only by default; set "
    "PROFINET_WRITES_ENABLED=true in the server's environment to allow write_io_data and write_device_by_alias."
)
CONFIG_CMD_TOOLS = "set_device_name, set_device_ip and identify_device"


def config_cmds_refusal(config: ProfinetConfig) -> str | None:
    """Why a DCP configuration command (DCP Set) is refused, or None if both switches are on.

    These commands change a device, so they need the general write switch *and* their own switch.
    """
    missing = [
        name
        for name, enabled in (
            ("PROFINET_WRITES_ENABLED", config.writes_enabled),
            ("PROFINET_CONFIG_CMDS_ENABLED", config.config_cmds_enabled),
        )
        if not enabled
    ]
    if not missing:
        return None
    return (
        f"DCP configuration commands are disabled, so nothing was sent. {CONFIG_CMD_TOOLS} need both "
        f"PROFINET_WRITES_ENABLED=true and PROFINET_CONFIG_CMDS_ENABLED=true in the server's environment; "
        f"not set: {', '.join(missing)}."
    )


def match_device(devices: list[dict[str, Any]], identifier: str) -> dict[str, Any] | None:
    """Find a device by station name (case-insensitive), IP address or MAC address."""
    wanted = identifier.strip().lower()
    wanted_mac = wanted.replace("-", ":")
    for device in devices:
        name = device.get("device_name")
        if isinstance(name, str) and name.lower() == wanted:
            return device
    for device in devices:
        if device.get("ip_address") == wanted or device.get("mac_address") == wanted_mac:
            return device
    return None


class ProfinetClient:
    """One object per server. The same :class:`ProfinetConfig` drives the tools and the client."""

    def __init__(self, config: ProfinetConfig, backend: Backend | None = None) -> None:
        self.config = config
        self.backend = backend if backend is not None else create_backend(config)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    async def _timed(
        self,
        operation: Callable[[], Awaitable[tuple[T, dict[str, Any]]]],
        *,
        what: str,
        changes_device: bool,
        timeout: float | None = None,
    ) -> tuple[T, dict[str, Any]]:
        """Run one backend request under an overall deadline (suite rule 12) and time it.

        The backend's own timeouts normally end a request first; this is the backstop so that no
        call can hang. On expiry a change is reported as possibly applied, a read as a clean error.
        """
        # The tool call's single absolute deadline, plus 2 s: a backstop behind the backend's own check.
        deadline = max(call_not_after(self.backend.deadline(timeout)) + 2.0 - time.monotonic(), 0.0)
        start = time.perf_counter()
        try:
            with anyio.fail_after(deadline):
                result, meta = await operation()
        except TimeoutError:
            if changes_device:
                raise OutcomeUnknown(
                    f"{what} did not finish within its {deadline:g} s deadline. The change may have been applied; "
                    "check before retrying (for example read it back). It was not sent again."
                ) from None
            raise ProfinetClientError(f"{what} did not finish within its {deadline:g} s deadline") from None
        meta = {"backend": self.backend.name, **meta}
        meta["duration_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
        return result, meta

    async def discover_devices(
        self, timeout: float | None = None, *, allow_cached: bool = False
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        wait = timeout if timeout is not None else self.config.dcp_timeout
        return await self._timed(
            lambda: self.backend.discover(wait, allow_cached=allow_cached),
            what="Discovery",
            changes_device=False,
            timeout=wait,
        )

    async def find_device(self, identifier: str, timeout: float | None = None) -> tuple[dict | None, dict[str, Any]]:
        """Look a device up by name, IP or MAC; may reuse a discovery answer that is only seconds old."""
        devices, meta = await self.discover_devices(timeout, allow_cached=True)
        return match_device(devices, identifier), meta

    async def read_io_data(
        self, device_name: str, slot: int, subslot: int, length: int
    ) -> tuple[bytes, dict[str, Any]]:
        return await self._timed(
            lambda: self.backend.read_io(device_name, slot, subslot, length), what="The read", changes_device=False
        )

    async def write_io_data(
        self, device_name: str, slot: int, subslot: int, data: bytes
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.config.writes_enabled:
            raise OperationRefused(WRITES_DISABLED)
        return await self._timed(
            lambda: self.backend.write_io(device_name, slot, subslot, data), what="The write", changes_device=True
        )

    async def identify_device(self, device_mac: str, duration_s: int) -> tuple[dict[str, Any], dict[str, Any]]:
        # DCP Set (Signal): a configuration command like set_device_name, behind the same switch.
        if refusal := config_cmds_refusal(self.config):
            raise OperationRefused(refusal)
        return await self._timed(
            lambda: self.backend.identify(device_mac, duration_s), what="identify", changes_device=True
        )

    async def set_device_name(self, device_mac: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if refusal := config_cmds_refusal(self.config):
            raise OperationRefused(refusal)
        return await self._timed(lambda: self.backend.set_name(device_mac, name), what="set_name", changes_device=True)

    async def set_device_ip(
        self, device_mac: str, ip_address: str, subnet_mask: str, gateway: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if refusal := config_cmds_refusal(self.config):
            raise OperationRefused(refusal)
        return await self._timed(
            lambda: self.backend.set_ip(device_mac, ip_address, subnet_mask, gateway),
            what="set_ip",
            changes_device=True,
        )

    def connection_status(self) -> dict[str, Any]:
        """Configured settings only; nothing is contacted."""
        status = {
            "backend": self.backend.name,
            "interface": self.config.interface,
            "controller_ip": self.config.controller_ip,
            "network": self.config.network_cidr,
            "dcp_port": self.config.dcp_port,
        }
        status.update(self.backend.describe())
        return status

    def shutdown(self) -> None:
        """The MCP client disconnected: stop pending device requests (see the backends)."""
        self.backend.shutdown()

    async def probe(self) -> dict[str, Any]:
        deadline = self.backend.deadline()
        try:
            with anyio.fail_after(deadline):
                return await self.backend.probe()
        except TimeoutError:
            return {"reachable": False, "detail": f"No answer within {deadline:g} s"}
