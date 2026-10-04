"""PROFINET client facade: safety switches, timing, and routing to the configured backend."""

from __future__ import annotations

import time
from collections.abc import Awaitable
from typing import Any, TypeVar

from .backends import (
    Backend,
    BackendNotConfigured,
    OperationNotImplemented,
    OperationRefused,
    OutcomeUnknown,
    ProfinetClientError,
    create_backend,
)
from .config import ProfinetConfig

__all__ = [
    "BackendNotConfigured",
    "OperationNotImplemented",
    "OperationRefused",
    "OutcomeUnknown",
    "ProfinetClient",
    "ProfinetClientError",
    "match_device",
]

T = TypeVar("T")

WRITES_DISABLED = (
    "Writes are disabled, so nothing was sent. The server is read-only by default; set "
    "PROFINET_WRITES_ENABLED=true in the server's environment to allow write_io_data and write_device_by_alias."
)
CONFIG_CMDS_DISABLED = (
    "DCP configuration commands are disabled, so nothing was sent. Set PROFINET_CONFIG_CMDS_ENABLED=true in the "
    "server's environment to allow set_device_name, set_device_ip and identify_device."
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

    async def _timed(self, operation: Awaitable[tuple[T, dict[str, Any]]]) -> tuple[T, dict[str, Any]]:
        start = time.perf_counter()
        result, meta = await operation
        meta = {"backend": self.backend.name, **meta}
        meta["duration_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
        return result, meta

    async def discover_devices(self, timeout: float | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        wait = timeout if timeout is not None else self.config.dcp_timeout
        return await self._timed(self.backend.discover(wait))

    async def find_device(self, identifier: str, timeout: float | None = None) -> tuple[dict | None, dict[str, Any]]:
        devices, meta = await self.discover_devices(timeout)
        return match_device(devices, identifier), meta

    async def read_io_data(
        self, device_name: str, slot: int, subslot: int, length: int
    ) -> tuple[bytes, dict[str, Any]]:
        return await self._timed(self.backend.read_io(device_name, slot, subslot, length))

    async def write_io_data(
        self, device_name: str, slot: int, subslot: int, data: bytes
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.config.writes_enabled:
            raise OperationRefused(WRITES_DISABLED)
        return await self._timed(self.backend.write_io(device_name, slot, subslot, data))

    async def identify_device(self, device_mac: str, duration_s: int) -> tuple[dict[str, Any], dict[str, Any]]:
        # DCP Set (Signal): a configuration command like set_device_name, behind the same switch.
        if not self.config.config_cmds_enabled:
            raise OperationRefused(CONFIG_CMDS_DISABLED)
        return await self._timed(self.backend.identify(device_mac, duration_s))

    async def set_device_name(self, device_mac: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.config.config_cmds_enabled:
            raise OperationRefused(CONFIG_CMDS_DISABLED)
        return await self._timed(self.backend.set_name(device_mac, name))

    async def set_device_ip(
        self, device_mac: str, ip_address: str, subnet_mask: str, gateway: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.config.config_cmds_enabled:
            raise OperationRefused(CONFIG_CMDS_DISABLED)
        return await self._timed(self.backend.set_ip(device_mac, ip_address, subnet_mask, gateway))

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

    async def probe(self) -> dict[str, Any]:
        return await self.backend.probe()
