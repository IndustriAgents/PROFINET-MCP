"""MCP tool definitions for PROFINET.

Every tool returns the IndustriConnect envelope ``{success, data, error, meta}``.
Errors - including bad arguments, refused writes and operations the selected
backend cannot perform - come back as ``success: false`` with a message; tools
never raise. ``meta.backend`` names the backend that answered (``mock`` or
``network``) for every tool that talks to devices.
"""

import functools
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field, ValidationError

from . import __version__, validation
from .backends import DeviceRejected, RateLimited, RequestNotSent
from .config import ProfinetConfig
from .gsd_parser import GSDMLError, GSDParser
from .pn_client import (
    WRITES_DISABLED,
    BackendNotConfigured,
    OperationNotImplemented,
    OperationRefused,
    OutcomeUnknown,
    ProfinetClient,
    ProfinetClientError,
    config_cmds_refusal,
)

log = logging.getLogger(__name__)

GSD_PARSE_DEADLINE_S = 30.0

Envelope = dict[str, Any]

# -- parameter types (the descriptions and limits end up in each tool's input schema) --

DeviceName = Annotated[
    str, Field(min_length=1, max_length=240, description="Station name (NameOfStation) of the device, e.g. line1-io")
]
DeviceRef = Annotated[
    str,
    Field(min_length=1, max_length=240, description="Station name, IP address or MAC address of the device"),
]
DeviceMac = Annotated[str, Field(min_length=1, max_length=64, description="MAC address, e.g. 00:11:22:33:44:55")]
# Numbers are strict: true, "1" or 2.0 are rejected rather than coerced into a slot or a byte value.
Slot = Annotated[
    int,
    Field(strict=True, ge=0, le=validation.MAX_SLOT, description="Slot number (slot 0 is the device access point)"),
]
Subslot = Annotated[int, Field(strict=True, ge=1, le=validation.MAX_SUBSLOT, description="Subslot number, usually 1")]
DataLength = Annotated[
    int,
    Field(strict=True, ge=1, le=validation.MAX_IO_BYTES, description=f"Bytes to read (1-{validation.MAX_IO_BYTES})"),
]
ByteValues = Annotated[
    list[Annotated[int, Field(strict=True, ge=0, le=255)]],
    Field(min_length=1, max_length=validation.MAX_IO_BYTES, description="Bytes to write, each an integer 0-255"),
]
DiscoveryTimeout = Annotated[
    float | None,
    Field(
        strict=True,
        ge=0.5,
        le=60,
        description="Seconds to wait for answers, 0.5-60 (default: PROFINET_DCP_TIMEOUT, 5)",
    ),
]
IdentifySeconds = Annotated[
    int, Field(strict=True, ge=1, le=600, description="How long the device should flash, in seconds")
]
StationName = Annotated[
    str, Field(min_length=1, max_length=240, description="New station name: lowercase a-z, 0-9, '-' and '.'")
]
IPv4 = Annotated[str, Field(min_length=7, max_length=15, description="IPv4 address, e.g. 192.168.1.20")]
Alias = Annotated[str, Field(min_length=1, max_length=200, description="Alias defined in DEVICE_MAP_FILE")]
FilePath = Annotated[str, Field(min_length=1, max_length=4096, description="Path of a GSDML (.xml) file")]

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
# Rule 13: nothing that changes a device advertises idempotency (it would invite blind retries).
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def envelope(success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None) -> Envelope:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}


#: Tools that change a device. Their envelopes always carry ``meta.outcome`` - "written" (confirmed),
#: "rejected" (the device refused it), "unknown" (sent, but no usable reply: it may have been applied)
#: or "not_sent" - and ``meta.request_sent`` (true, false, or null when it cannot be known).
CHANGE_TOOLS = frozenset(
    {"write_io_data", "write_device_by_alias", "set_device_name", "set_device_ip", "identify_device"}
)


def outcome(tool: str, result: str, request_sent: bool | None) -> dict[str, Any]:
    return {"outcome": result, "request_sent": request_sent} if tool in CHANGE_TOOLS else {}


def invalid_arguments(tool: str, exc: ValidationError) -> Envelope:
    """Envelope for arguments that failed schema validation (wrong type, out of range, missing)."""
    problems = [
        {"argument": ".".join(str(part) for part in err.get("loc", ())) or "arguments", "problem": err.get("msg")}
        for err in exc.errors(include_url=False)
    ]
    message = "; ".join(f"{p['argument']}: {p['problem']}" for p in problems)
    meta = {"tool": tool, **outcome(tool, "not_sent", False), "invalid_arguments": problems}
    return envelope(False, error=f"Invalid arguments: {message}", meta=meta)


class DeviceMap:
    """Aliases from ``DEVICE_MAP_FILE``, re-read whenever the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.error: str | None = None
        self._devices: dict[str, Any] = {}
        self._stamp: tuple[int, int] | None = None
        self.refresh()

    def refresh(self) -> None:
        if not self.path:
            self._devices, self._stamp, self.error = {}, None, None
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._devices, self._stamp = {}, None
            self.error = f"DEVICE_MAP_FILE {self.path} cannot be read: {exc.strerror or exc}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._devices = {}
            self.error = f"DEVICE_MAP_FILE {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict):
            self._devices = {}
            self.error = f"DEVICE_MAP_FILE {self.path} must hold a JSON object mapping aliases to devices"
            return
        self._devices = {str(name): spec for name, spec in data.items()}
        bad = sorted(name for name, spec in self._devices.items() if not isinstance(spec, dict))
        self.error = f"DEVICE_MAP_FILE entries that are not JSON objects: {', '.join(bad)}" if bad else None

    def get(self, alias: str) -> Any:
        self.refresh()
        return self._devices.get(alias)

    def list(self) -> list[dict[str, Any]]:
        self.refresh()
        return [
            {
                "alias": alias,
                "device_name": spec.get("device_name"),
                "ip_address": spec.get("ip_address"),
                "description": spec.get("description"),
                "slot": spec.get("slot"),
                "subslot": spec.get("subslot", 1),
                "data_length": spec.get("data_length", 8),
            }
            for alias, spec in self._devices.items()
            if isinstance(spec, dict)
        ]

    def count(self) -> int:
        return len(self.list())


@dataclass(slots=True)
class ToolResources:
    client: ProfinetClient
    config: ProfinetConfig
    device_map: DeviceMap
    gsd_parser: GSDParser


def register_tools(server: FastMCP, resources: ToolResources) -> None:
    client = resources.client
    config = resources.config

    def ok(tool: str, data: Any, meta: dict[str, Any] | None = None) -> Envelope:
        return envelope(True, data=data, meta={"tool": tool, **(meta or {})})

    def err(tool: str, message: str, meta: dict[str, Any] | None = None) -> Envelope:
        return envelope(False, error=message, meta={"tool": tool, **(meta or {})})

    def backend_meta(**extra: Any) -> dict[str, Any]:
        return {"backend": client.backend_name, **extra}

    def guarded(fn: Callable[..., Awaitable[Envelope]]) -> Callable[..., Awaitable[Envelope]]:
        """Turn every exception into a ``success: false`` envelope."""
        tool = fn.__name__

        not_sent = outcome(tool, "not_sent", False)

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Envelope:
            try:
                result = await fn(*args, **kwargs)
            except validation.InvalidArgument as exc:
                return err(tool, f"Invalid arguments: {exc}", not_sent)
            except RateLimited as exc:
                retry = round(exc.retry_after, 1)
                return err(tool, str(exc), backend_meta(refused=True, retry_after_s=retry, **not_sent))
            except OperationRefused as exc:
                return err(tool, str(exc), backend_meta(refused=True, **not_sent))
            except OperationNotImplemented as exc:
                return err(tool, str(exc), backend_meta(not_implemented=True, **not_sent))
            except (BackendNotConfigured, RequestNotSent) as exc:
                return err(tool, str(exc), backend_meta(**not_sent))
            except DeviceRejected as exc:
                return err(tool, str(exc), backend_meta(**outcome(tool, "rejected", True)))
            except OutcomeUnknown as exc:
                return err(tool, str(exc), backend_meta(outcome_unknown=True, **outcome(tool, "unknown", True)))
            except ProfinetClientError as exc:
                return err(tool, str(exc), backend_meta(**outcome(tool, "unknown", None)))
            except Exception as exc:  # never let a tool call crash the session
                log.exception("Tool %s failed", tool)
                return err(tool, f"Internal error: {type(exc).__name__}: {exc}", outcome(tool, "unknown", None))
            if result.get("success"):
                result["meta"].update(outcome(tool, "written", True))
            return result

        return wrapper

    def tool(annotations: ToolAnnotations) -> Callable[[Callable[..., Awaitable[Envelope]]], Any]:
        def decorate(fn: Callable[..., Awaitable[Envelope]]) -> Any:
            return server.tool(annotations=annotations)(guarded(fn))

        return decorate

    async def read(tool_name: str, device_name: str, slot: int, subslot: int, length: int, **extra: Any) -> Envelope:
        device_name = validation.device_identifier(device_name)
        payload, meta = await client.read_io_data(device_name, slot, subslot, length)
        data = {
            **extra,
            "device_name": device_name,
            "slot": slot,
            "subslot": subslot,
            "data_length": len(payload),
            "raw_data_hex": payload.hex(),
            "bytes": list(payload),
        }
        return ok(tool_name, data, meta)

    async def write(
        tool_name: str, device_name: str, slot: int, subslot: int, data: list[int], **extra: Any
    ) -> Envelope:
        device_name = validation.device_identifier(device_name)
        payload = bytes(data)  # values were range-checked by the schema
        result, meta = await client.write_io_data(device_name, slot, subslot, payload)
        return ok(
            tool_name,
            {**extra, "device_name": device_name, "slot": slot, "subslot": subslot, **result},
            meta,
        )

    def alias_spec(alias: str) -> tuple[str, dict[str, Any]]:
        spec = resources.device_map.get(alias)
        if spec is None:
            known = ", ".join(sorted(a["alias"] for a in resources.device_map.list())) or "none"
            hint = f" ({resources.device_map.error})" if resources.device_map.error else ""
            raise validation.InvalidArgument(f"unknown alias {alias!r}; known aliases: {known}{hint}")
        if not isinstance(spec, dict):
            raise validation.InvalidArgument(f"alias {alias!r} in DEVICE_MAP_FILE is not a JSON object")
        device_name = spec.get("device_name") or alias
        return validation.device_identifier(str(device_name), f"alias {alias!r} device_name"), spec

    # -- discovery ---------------------------------------------------------------------

    @tool(READ)
    async def discover_devices(timeout: DiscoveryTimeout = None) -> Envelope:
        """Discover PROFINET IO devices.

        Network backend: multicasts a DCP Identify All request on PROFINET_INTERFACE and lists every
        device that answers within `timeout` seconds (needs raw-socket privileges). Mock backend: lists
        the devices simulated by profinet-mock-server. Each device reports device_name, mac_address,
        ip_address, subnet_mask, gateway, vendor_id, device_id, device_type and type_of_station where
        known (null otherwise). meta.backend says which backend answered.
        """
        devices, meta = await client.discover_devices(timeout)
        return ok("discover_devices", {"devices": devices, "count": len(devices)}, meta)

    @tool(READ)
    async def get_device_info(device_name: DeviceRef) -> Envelope:
        """Discovery details for one device, looked up by station name, IP address or MAC address.

        Runs discovery (see discover_devices) and returns the matching device, or success=false if no
        device with that name or address answered.
        """
        wanted = validation.device_identifier(device_name)
        device, meta = await client.find_device(wanted)
        if device is None:
            return err("get_device_info", f"No device named or addressed {wanted!r} answered discovery", meta)
        return ok("get_device_info", device, meta)

    @tool(READ)
    async def test_device_communication(device_name: DeviceRef) -> Envelope:
        """Check that a device answers, by station name, IP address or MAC address.

        Uses discovery (DCP Identify on the network backend), so it works for any IO device without
        knowing its modules. Returns reachable=true with the round trip in meta.duration_ms, or
        success=false if the device did not answer.
        """
        wanted = validation.device_identifier(device_name)
        device, meta = await client.find_device(wanted)
        if device is None:
            return err("test_device_communication", f"Device {wanted!r} did not answer", {**meta, "reachable": False})
        return ok(
            "test_device_communication",
            {"device_name": device.get("device_name"), "reachable": True, "device": device},
            meta,
        )

    # -- DCP configuration ---------------------------------------------------------------

    @tool(WRITE)
    async def identify_device(device_mac: DeviceMac, duration_s: IdentifySeconds = 5) -> Envelope:
        """Make a device flash its identification LED (DCP Set Signal) so it can be found in the cabinet.

        A DCP configuration command: refused unless both PROFINET_WRITES_ENABLED=true and
        PROFINET_CONFIG_CMDS_ENABLED=true, like set_device_name. Supported by the mock backend. The
        network backend does not implement DCP Set and returns success=false without sending anything.
        """
        if refusal := config_cmds_refusal(config):
            raise OperationRefused(refusal)
        mac = validation.mac_address(device_mac)
        result, meta = await client.identify_device(mac, duration_s)
        return ok("identify_device", result, meta)

    @tool(WRITE)
    async def set_device_name(device_mac: DeviceMac, name: StationName) -> Envelope:
        """Assign a new station name (DCP Set NameOfStation) to the device with this MAC address.

        Refused unless both PROFINET_WRITES_ENABLED=true and PROFINET_CONFIG_CMDS_ENABLED=true:
        renaming a device in a running plant takes it away from the IO controller that expects it.
        The name must follow the PROFINET rules (lowercase a-z, 0-9, '-', dot-separated labels of
        1-63 characters, at most 240 in total). Supported by the mock backend; the network backend
        returns success=false without sending anything.
        """
        if refusal := config_cmds_refusal(config):
            raise OperationRefused(refusal)
        mac = validation.mac_address(device_mac)
        new_name = validation.station_name(name)
        result, meta = await client.set_device_name(mac, new_name)
        return ok("set_device_name", result, meta)

    @tool(WRITE)
    async def set_device_ip(
        device_mac: DeviceMac,
        ip_address: IPv4,
        subnet_mask: IPv4,
        gateway: Annotated[str | None, Field(description="Default gateway; omit or 0.0.0.0 for none")] = None,
    ) -> Envelope:
        """Assign IP address, subnet mask and gateway (DCP Set IP) to the device with this MAC address.

        Refused unless both PROFINET_WRITES_ENABLED=true and PROFINET_CONFIG_CMDS_ENABLED=true. The
        gateway, if given, must be inside the subnet. Supported by the mock backend; the network
        backend returns success=false without sending anything.
        """
        if refusal := config_cmds_refusal(config):
            raise OperationRefused(refusal)
        mac = validation.mac_address(device_mac)
        ip, mask, gw = validation.ip_settings(ip_address, subnet_mask, gateway)
        result, meta = await client.set_device_ip(mac, ip, mask, gw)
        return ok("set_device_ip", result, meta)

    # -- I/O data ------------------------------------------------------------------------

    @tool(READ)
    async def read_io_data(device_name: DeviceName, slot: Slot, subslot: Subslot, data_length: DataLength) -> Envelope:
        """Read I/O data from a slot/subslot of a device; returns raw_data_hex and the byte values.

        Supported by the mock backend. The network backend returns success=false: cyclic PROFINET IO
        needs an IO controller with an application relationship, which this server does not have.
        """
        return await read("read_io_data", device_name, slot, subslot, data_length)

    @tool(WRITE)
    async def write_io_data(device_name: DeviceName, slot: Slot, subslot: Subslot, data: ByteValues) -> Envelope:
        """Write bytes to an output slot/subslot of a device. This changes physical outputs.

        Refused unless PROFINET_WRITES_ENABLED=true (the server is read-only by default). Supported by
        the mock backend; the network backend returns success=false without sending anything.
        """
        if not config.writes_enabled:
            raise OperationRefused(WRITES_DISABLED)
        return await write("write_io_data", device_name, slot, subslot, data)

    # -- device map aliases --------------------------------------------------------------

    @tool(LOCAL)
    async def list_devices() -> Envelope:
        """List the aliases defined in DEVICE_MAP_FILE (device name, address, slot, subslot, length).

        Reads only the local file; no device is contacted.
        """
        meta = {"source": "device_map", "device_map_file": str(resources.device_map.path or "") or None}
        entries = resources.device_map.list()
        if resources.device_map.error and not entries:
            return err("list_devices", resources.device_map.error, meta)
        data: dict[str, Any] = {"devices": entries, "count": len(entries)}
        if resources.device_map.error:
            data["warning"] = resources.device_map.error
        return ok("list_devices", data, meta)

    @tool(READ)
    async def read_device_by_alias(alias: Alias) -> Envelope:
        """read_io_data for the device, slot, subslot and length behind an alias from DEVICE_MAP_FILE."""
        device_name, spec = alias_spec(alias)
        source = f"alias {alias!r}"
        slot, subslot = validation.io_address(spec.get("slot"), spec.get("subslot", 1), source=source)
        length = validation.data_length(spec.get("data_length", 8), source=source)
        return await read("read_device_by_alias", device_name, slot, subslot, length, alias=alias)

    @tool(WRITE)
    async def write_device_by_alias(alias: Alias, data: ByteValues) -> Envelope:
        """write_io_data for the device, slot and subslot behind an alias from DEVICE_MAP_FILE.

        Refused unless PROFINET_WRITES_ENABLED=true, like write_io_data.
        """
        if not config.writes_enabled:
            raise OperationRefused(WRITES_DISABLED)
        device_name, spec = alias_spec(alias)
        slot, subslot = validation.io_address(spec.get("slot"), spec.get("subslot", 1), source=f"alias {alias!r}")
        return await write("write_device_by_alias", device_name, slot, subslot, data, alias=alias)

    # -- GSDML -----------------------------------------------------------------------------

    @tool(LOCAL)
    async def load_gsd_file(filepath: FilePath) -> Envelope:
        """Parse a GSDML device description file: device identity, access points and modules.

        Returns metadata (vendor, vendor/device IDs, device name, GSDML version), the device access
        points with their usable modules, every module with its fixed submodules and its pluggable
        submodules (resolved from the SubmoduleList; unresolved references are flagged and the module
        gets submodules_incomplete=true), and input/output sizes in bytes (null when a data type's size
        is unknown). Relative paths are also looked up in
        PROFINET_GSD_PATH. Files are cached and re-parsed when they change on disk.
        """
        meta: dict[str, Any] = {"source": "gsdml_file", "filepath": filepath}
        try:
            # In a worker thread, under a deadline, so a large file cannot stall the server (rule 12).
            with anyio.fail_after(GSD_PARSE_DEADLINE_S):
                entry, cached = await anyio.to_thread.run_sync(
                    resources.gsd_parser.load_cached, filepath, abandon_on_cancel=True
                )
        except TimeoutError:
            return err("load_gsd_file", f"Parsing {filepath} took longer than {GSD_PARSE_DEADLINE_S:g} s", meta)
        except FileNotFoundError as exc:
            return err("load_gsd_file", str(exc), meta)
        except GSDMLError as exc:
            return err("load_gsd_file", str(exc), meta)
        except OSError as exc:
            return err("load_gsd_file", f"Cannot read {filepath}: {exc.strerror or exc}", meta)
        meta["cached"] = cached
        return ok(
            "load_gsd_file",
            {
                "filepath": str(entry.path),
                "metadata": entry.metadata,
                "device_access_points": entry.device_access_points,
                "modules": entry.modules,
                "module_count": len(entry.modules),
                "submodules": entry.submodules,
            },
            meta,
        )

    # -- status ----------------------------------------------------------------------------

    @tool(LOCAL)
    async def ping() -> Envelope:
        """Server health and settings: version, backend, connection settings and safety switches.

        Does not contact any device; use get_connection_status to check the backend.
        """
        return ok(
            "ping",
            {
                "server": "profinet-mcp",
                "version": __version__,
                "backend": client.backend_name,
                "connection": client.connection_status(),
                "writes_enabled": config.writes_enabled,
                "config_cmds_enabled": config.config_cmds_enabled,
                "device_aliases": resources.device_map.count(),
                "device_map_error": resources.device_map.error,
            },
            backend_meta(contacted=False),
        )

    @tool(READ)
    async def get_connection_status() -> Envelope:
        """Connection settings plus a reachability check of the backend.

        Mock backend: opens and closes a TCP connection to the mock (reachable true/false). Network
        backend: not checked (reachable null) - run discover_devices instead. No backend: reachable null.
        """
        status = client.connection_status()
        probe = await client.probe()
        status.update(reachable=probe.get("reachable"), detail=probe.get("detail"))
        return ok("get_connection_status", status, backend_meta())
