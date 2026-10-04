"""Backends behind :class:`~profinet_mcp.pn_client.ProfinetClient`.

``PROFINET_BACKEND`` picks one:

* ``none`` (default) - nothing is contacted; device tools say how to choose a backend.
* ``mock`` - JSON-lines over TCP to ``profinet-mock-server`` (every tool works).
* ``network`` - the real PROFINET network on ``PROFINET_INTERFACE``. Only DCP
  Identify (discovery) is implemented; I/O and DCP Set return "not implemented".

Every backend answers with ``(result, meta)``; ``meta["backend"]`` names it.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import anyio
from anyio.streams.buffered import BufferedByteReceiveStream

from . import dcp
from .config import BACKEND_MOCK, BACKEND_NETWORK, BACKEND_NONE, ProfinetConfig

MAX_RESPONSE_BYTES = 1024 * 1024

DEVICE_FIELDS = (
    "device_name",
    "mac_address",
    "ip_address",
    "subnet_mask",
    "gateway",
    "vendor",
    "vendor_id",
    "device_id",
    "device_type",
    "type_of_station",
)


class ProfinetClientError(RuntimeError):
    """A PROFINET operation failed; the message is shown to the model."""


class OperationNotImplemented(ProfinetClientError):
    """The selected backend cannot do this (yet)."""


class BackendNotConfigured(ProfinetClientError):
    """``PROFINET_BACKEND`` is unset or ``none``."""


class OperationRefused(ProfinetClientError):
    """Refused by a safety switch (writes or configuration commands disabled)."""


Meta = dict[str, Any]


class Backend(Protocol):
    name: str

    async def discover(self, timeout: float) -> tuple[list[dict[str, Any]], Meta]: ...

    async def read_io(self, device: str, slot: int, subslot: int, length: int) -> tuple[bytes, Meta]: ...

    async def write_io(self, device: str, slot: int, subslot: int, data: bytes) -> tuple[dict[str, Any], Meta]: ...

    async def identify(self, mac: str, duration_s: int) -> tuple[dict[str, Any], Meta]: ...

    async def set_name(self, mac: str, name: str) -> tuple[dict[str, Any], Meta]: ...

    async def set_ip(self, mac: str, ip: str, subnet: str, gateway: str) -> tuple[dict[str, Any], Meta]: ...

    def describe(self) -> dict[str, Any]: ...

    async def probe(self) -> dict[str, Any]: ...


def create_backend(config: ProfinetConfig) -> Backend:
    if config.backend == BACKEND_MOCK:
        return MockBackend(
            host=config.mock_host,
            port=config.mock_port,
            timeout=config.timeout,
            max_retries=config.max_retries,
            backoff_base=config.retry_backoff_base,
        )
    if config.backend == BACKEND_NETWORK:
        return NetworkBackend(interface=config.interface)
    return UnconfiguredBackend()


# ---------------------------------------------------------------------------
# none
# ---------------------------------------------------------------------------

NO_BACKEND = (
    "No backend selected, so no device was contacted. Set PROFINET_BACKEND=mock to use the mock IO device "
    "(profinet-mock-server), or PROFINET_BACKEND=network for DCP discovery on PROFINET_INTERFACE "
    "(needs raw-socket privileges)."
)


class UnconfiguredBackend:
    name = BACKEND_NONE

    def _refuse(self) -> Any:
        raise BackendNotConfigured(NO_BACKEND)

    async def discover(self, timeout: float) -> tuple[list[dict[str, Any]], Meta]:
        return self._refuse()

    async def read_io(self, device: str, slot: int, subslot: int, length: int) -> tuple[bytes, Meta]:
        return self._refuse()

    async def write_io(self, device: str, slot: int, subslot: int, data: bytes) -> tuple[dict[str, Any], Meta]:
        return self._refuse()

    async def identify(self, mac: str, duration_s: int) -> tuple[dict[str, Any], Meta]:
        return self._refuse()

    async def set_name(self, mac: str, name: str) -> tuple[dict[str, Any], Meta]:
        return self._refuse()

    async def set_ip(self, mac: str, ip: str, subnet: str, gateway: str) -> tuple[dict[str, Any], Meta]:
        return self._refuse()

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name}

    async def probe(self) -> dict[str, Any]:
        return {"reachable": None, "detail": NO_BACKEND}


# ---------------------------------------------------------------------------
# mock: JSON lines over TCP to profinet-mock-server
# ---------------------------------------------------------------------------


class MockBackend:
    """Client for the JSON-lines protocol of ``profinet-mock-server``."""

    name = BACKEND_MOCK

    def __init__(
        self, host: str, port: int, timeout: float = 10.0, max_retries: int = 3, backoff_base: float = 0.5
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base

    @property
    def endpoint(self) -> str:
        return f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "mock_endpoint": self.endpoint}

    def _meta(self, op: str) -> Meta:
        return {"backend": self.name, "mock_endpoint": self.endpoint, "mock_op": op}

    async def _connect(self) -> Any:
        last: BaseException | None = None
        attempts = self.max_retries + 1
        for attempt in range(attempts):
            try:
                with anyio.fail_after(self.timeout):
                    return await anyio.connect_tcp(self.host, self.port)
            except (OSError, TimeoutError) as exc:
                last = exc
                if attempt + 1 < attempts:
                    await anyio.sleep(self.backoff_base * (2**attempt))
        raise ProfinetClientError(
            f"Cannot reach the mock IO device at {self.endpoint} ({last or 'timeout'}; {attempts} attempt(s)). "
            "Start it with `uv run --directory profinet-mock-server profinet-mock-server`, or check "
            "PROFINET_MOCK_HOST / PROFINET_MOCK_PORT."
        )

    async def call(self, request: dict[str, Any], timeout: float | None = None) -> Any:
        """Send one request and return the ``data`` of a successful answer."""
        op = request["op"]
        stream = await self._connect()
        try:
            async with stream:
                with anyio.fail_after(timeout or self.timeout):
                    await stream.send(json.dumps(request).encode("utf-8") + b"\n")
                    line = await BufferedByteReceiveStream(stream).receive_until(b"\n", MAX_RESPONSE_BYTES)
        except TimeoutError:
            raise ProfinetClientError(f"The mock at {self.endpoint} did not answer {op!r} in time") from None
        except (OSError, anyio.EndOfStream, anyio.BrokenResourceError, anyio.IncompleteRead) as exc:
            raise ProfinetClientError(f"Lost the connection to the mock at {self.endpoint}: {exc!r}") from None
        except anyio.DelimiterNotFound:
            raise ProfinetClientError(
                f"The mock's answer to {op!r} was larger than {MAX_RESPONSE_BYTES} bytes"
            ) from None
        try:
            response = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProfinetClientError(f"The mock answered {op!r} with something that is not JSON") from None
        if not isinstance(response, dict) or not isinstance(response.get("success"), bool):
            raise ProfinetClientError(f"The mock answered {op!r} without a success flag")
        if not response["success"]:
            raise ProfinetClientError(f"Mock IO device: {response.get('error') or 'request failed'}")
        return response.get("data")

    @staticmethod
    def _device_record(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or not isinstance(raw.get("device_name"), str):
            raise ProfinetClientError("The mock returned a malformed device entry")
        record = {key: raw.get(key) for key in DEVICE_FIELDS}
        if "io_areas" in raw:
            record["io_areas"] = raw["io_areas"]
        if "identifying" in raw:
            record["identifying"] = raw["identifying"]
        return record

    async def discover(self, timeout: float) -> tuple[list[dict[str, Any]], Meta]:
        data = await self.call({"op": "discover"}, timeout=timeout)
        if not isinstance(data, list):
            raise ProfinetClientError("The mock's discover answer is not a list")
        return [self._device_record(item) for item in data], self._meta("discover")

    async def read_io(self, device: str, slot: int, subslot: int, length: int) -> tuple[bytes, Meta]:
        data = await self.call({"op": "read", "device": device, "slot": slot, "subslot": subslot, "length": length})
        try:
            payload = bytes.fromhex(data["raw_data_hex"])
        except (TypeError, KeyError, ValueError):
            raise ProfinetClientError("The mock's read answer has no valid raw_data_hex") from None
        if len(payload) != length:
            raise ProfinetClientError(f"The mock returned {len(payload)} bytes, expected {length}")
        meta = self._meta("read")
        if isinstance(data, dict) and data.get("direction"):
            meta["direction"] = data["direction"]
        return payload, meta

    async def write_io(self, device: str, slot: int, subslot: int, data: bytes) -> tuple[dict[str, Any], Meta]:
        answer = await self.call(
            {"op": "write", "device": device, "slot": slot, "subslot": subslot, "data": list(data)}
        )
        written = answer.get("written_bytes") if isinstance(answer, dict) else None
        if written != len(data):
            raise ProfinetClientError(f"The mock confirmed {written!r} written bytes, expected {len(data)}")
        return {"written_bytes": written}, self._meta("write")

    async def identify(self, mac: str, duration_s: int) -> tuple[dict[str, Any], Meta]:
        answer = await self.call({"op": "identify", "mac": mac, "duration_s": duration_s})
        return self._confirmed(answer, mac), self._meta("identify")

    async def set_name(self, mac: str, name: str) -> tuple[dict[str, Any], Meta]:
        answer = await self.call({"op": "set_name", "mac": mac, "name": name})
        result = self._confirmed(answer, mac)
        if result.get("device_name") != name:
            raise ProfinetClientError(f"The mock did not confirm the new name {name!r}")
        return result, self._meta("set_name")

    async def set_ip(self, mac: str, ip: str, subnet: str, gateway: str) -> tuple[dict[str, Any], Meta]:
        answer = await self.call({"op": "set_ip", "mac": mac, "ip": ip, "subnet": subnet, "gateway": gateway})
        result = self._confirmed(answer, mac)
        if (result.get("ip_address"), result.get("subnet_mask"), result.get("gateway")) != (ip, subnet, gateway):
            raise ProfinetClientError("The mock did not confirm the requested IP settings")
        return result, self._meta("set_ip")

    @staticmethod
    def _confirmed(answer: Any, mac: str) -> dict[str, Any]:
        if not isinstance(answer, dict) or answer.get("mac_address") != mac:
            raise ProfinetClientError(f"The mock did not confirm the operation for {mac}")
        return answer

    async def probe(self) -> dict[str, Any]:
        try:
            with anyio.fail_after(min(self.timeout, 5.0)):
                stream = await anyio.connect_tcp(self.host, self.port)
            await stream.aclose()
        except (OSError, TimeoutError) as exc:
            return {"reachable": False, "detail": f"TCP connect to {self.endpoint} failed: {exc or 'timeout'}"}
        return {"reachable": True, "detail": f"TCP connect to {self.endpoint} succeeded"}


# ---------------------------------------------------------------------------
# network: the real PROFINET network (DCP Identify only)
# ---------------------------------------------------------------------------

IO_NOT_IMPLEMENTED = (
    "Not implemented on the network backend: cyclic PROFINET IO data is exchanged inside an application "
    "relationship (AR) owned by an IO controller, and this server has no IO-controller stack. Nothing was "
    "sent. Use PROFINET_BACKEND=mock to exercise this tool against profinet-mock-server."
)
DCP_SET_NOT_IMPLEMENTED = (
    "Not implemented on the network backend: DCP Set ({what}) has not been implemented or verified against "
    "real devices, so nothing was sent. Use a PROFINET engineering tool for commissioning, or "
    "PROFINET_BACKEND=mock to exercise this tool."
)


class NetworkBackend:
    """Real network. Discovery uses DCP Identify All; everything else is refused honestly."""

    name = BACKEND_NETWORK

    def __init__(self, interface: str, transport: dcp.L2Transport | None = None) -> None:
        self.interface = interface
        self.transport = transport if transport is not None else dcp.ScapyL2Transport(interface)

    def describe(self) -> dict[str, Any]:
        return {"backend": self.name, "interface": self.interface}

    def _meta(self) -> Meta:
        return {"backend": self.name, "interface": self.interface}

    async def discover(self, timeout: float) -> tuple[list[dict[str, Any]], Meta]:
        try:
            devices, info = await anyio.to_thread.run_sync(dcp.identify_all, self.transport, timeout)
        except dcp.DcpError as exc:
            raise ProfinetClientError(str(exc)) from exc
        meta = self._meta()
        meta.update(info)
        meta["timeout_s"] = timeout
        return devices, meta

    async def read_io(self, device: str, slot: int, subslot: int, length: int) -> tuple[bytes, Meta]:
        raise OperationNotImplemented(IO_NOT_IMPLEMENTED)

    async def write_io(self, device: str, slot: int, subslot: int, data: bytes) -> tuple[dict[str, Any], Meta]:
        raise OperationNotImplemented(IO_NOT_IMPLEMENTED)

    async def identify(self, mac: str, duration_s: int) -> tuple[dict[str, Any], Meta]:
        raise OperationNotImplemented(DCP_SET_NOT_IMPLEMENTED.format(what="identification signal"))

    async def set_name(self, mac: str, name: str) -> tuple[dict[str, Any], Meta]:
        raise OperationNotImplemented(DCP_SET_NOT_IMPLEMENTED.format(what="name of station"))

    async def set_ip(self, mac: str, ip: str, subnet: str, gateway: str) -> tuple[dict[str, Any], Meta]:
        raise OperationNotImplemented(DCP_SET_NOT_IMPLEMENTED.format(what="IP parameters"))

    async def probe(self) -> dict[str, Any]:
        return {
            "reachable": None,
            "detail": "Not checked: the network backend only sends DCP Identify, which discover_devices does.",
        }
