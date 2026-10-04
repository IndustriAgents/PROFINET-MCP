"""Mock PROFINET IO device for local development and integration tests.

The mock does not speak PROFINET on the wire. It answers one JSON object per
line over TCP, with one JSON line back, so the MCP server (``PROFINET_BACKEND=mock``)
and tests can exercise every tool without hardware or raw-socket privileges.

Requests (``op`` selects the operation):

* ``{"op": "discover"}`` - list the simulated devices (DCP Identify All).
* ``{"op": "read", "device": NAME, "slot": 1, "subslot": 1, "length": 8}`` -
  read an I/O area; ``length`` is optional and defaults to the whole area.
* ``{"op": "write", "device": NAME, "slot": 1, "subslot": 2, "data": [1, 2]}`` -
  write the start of an *output* area.
* ``{"op": "identify", "mac": MAC, "duration_s": 5}`` - DCP Signal (flash LED).
* ``{"op": "set_name", "mac": MAC, "name": NEW_NAME}`` - DCP Set NameOfStation.
* ``{"op": "set_ip", "mac": MAC, "ip": IP, "subnet": MASK, "gateway": GW}`` -
  DCP Set IP parameters (``gateway`` optional, ``0.0.0.0`` means none).

Every answer is ``{"success": true, "data": ...}`` or
``{"success": false, "error": "..."}``.
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import random
import re
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv
from rich.console import Console

console = Console()

PROTOCOL_VERSION = 2
MAX_LINE_BYTES = 64 * 1024
MAX_AREA_BYTES = 1440
MAX_SLOT = 0x7FFF
MAX_SUBSLOT = 0xFFFF
MAX_IDENTIFY_SECONDS = 600

TRUE_VALUES = {"1", "true", "t", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "f", "no", "n", "off"}

_MAC_RE = re.compile(r"^[0-9a-f]{2}([:-])[0-9a-f]{2}(\1[0-9a-f]{2}){4}$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_PORT_NAME_RE = re.compile(r"^port-\d{3}(-\d{5})?$")
_IP_LIKE_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")

DEFAULT_DEVICES: list[dict[str, Any]] = [
    {
        "name": "pn-device-01",
        "mac": "00:11:22:33:44:55",
        "ip": "192.168.1.100",
        "subnet": "255.255.255.0",
        "gateway": "0.0.0.0",
        "vendor": "MockVendor",
        "device_type": "IO Device",
        "type_of_station": "Mock IO device",
        "areas": [
            {"slot": 1, "subslot": 1, "length": 8, "direction": "input"},
            {"slot": 1, "subslot": 2, "length": 4, "direction": "output"},
        ],
    }
]


class MockError(ValueError):
    """A request or a devices file the mock cannot accept."""


class ConfigError(ValueError):
    """An unusable setting (environment variable or command-line flag)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def env_bool(environ: Mapping[str, str], key: str, default: bool) -> bool:
    raw = environ.get(key)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(f"{key}={raw!r} is not a boolean (use true or false)")


def _env_number(environ: Mapping[str, str], key: str, default: str, kind: type) -> Any:
    raw = environ.get(key) or default
    try:
        return kind(raw)
    except ValueError:
        raise ConfigError(f"{key}={raw!r} is not a valid {kind.__name__}") from None


@dataclass
class MockConfig:
    host: str = "127.0.0.1"
    port: int = 5600
    update_interval: float = 1.0
    verbose: bool = False
    devices_file: Path | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> MockConfig:
        """Read ``MOCK_PROFINET_*`` settings; call after ``load_dotenv()``."""
        env = os.environ if environ is None else environ
        devices_file = env.get("MOCK_PROFINET_DEVICES_FILE") or None
        return cls(
            host=env.get("MOCK_PROFINET_HOST") or "127.0.0.1",
            port=_env_number(env, "MOCK_PROFINET_PORT", "5600", int),
            update_interval=_env_number(env, "MOCK_PROFINET_UPDATE_INTERVAL", "1.0", float),
            verbose=env_bool(env, "MOCK_PROFINET_VERBOSE", False),
            devices_file=Path(devices_file).expanduser() if devices_file else None,
        )


# ---------------------------------------------------------------------------
# Device model
# ---------------------------------------------------------------------------


@dataclass
class IOArea:
    slot: int
    subslot: int
    length: int
    direction: str  # "input" (device -> controller) or "output" (controller -> device)
    buffer: bytearray = field(default_factory=bytearray)

    def __post_init__(self) -> None:
        if not self.buffer:
            self.buffer = bytearray(self.length)

    @property
    def key(self) -> str:
        return f"{self.slot}:{self.subslot}"

    def describe(self) -> dict[str, Any]:
        return {"slot": self.slot, "subslot": self.subslot, "length": self.length, "direction": self.direction}


@dataclass
class MockDevice:
    name: str
    mac: str
    ip: str
    subnet: str = "255.255.255.0"
    gateway: str = "0.0.0.0"
    vendor: str | None = None
    vendor_id: str | None = None
    device_id: str | None = None
    device_type: str = "IO Device"
    type_of_station: str | None = None
    areas: dict[str, IOArea] = field(default_factory=dict)
    identify_until: float | None = None

    def describe(self) -> dict[str, Any]:
        identifying = self.identify_until is not None and self.identify_until > time.time()
        return {
            "device_name": self.name,
            "mac_address": self.mac,
            "ip_address": self.ip,
            "subnet_mask": self.subnet,
            "gateway": self.gateway,
            "vendor": self.vendor,
            "vendor_id": self.vendor_id,
            "device_id": self.device_id,
            "device_type": self.device_type,
            "type_of_station": self.type_of_station,
            "identifying": identifying,
            "io_areas": [area.describe() for area in self.areas.values()],
        }


def normalize_mac(value: Any) -> str:
    if not isinstance(value, str) or not _MAC_RE.match(value.strip().lower()):
        raise MockError(f"Invalid MAC address {value!r}; expected six hex octets such as 00:11:22:33:44:55")
    return value.strip().lower().replace("-", ":")


def check_station_name(name: Any) -> str:
    """Apply the PROFINET NameOfStation rules a real device enforces on DCP Set."""
    if not isinstance(name, str) or not name:
        raise MockError("name must be a non-empty string")
    if len(name) > 240:
        raise MockError("name is longer than 240 characters")
    if name != name.lower():
        raise MockError("name must be lowercase (PROFINET station names are DNS-style)")
    labels = name.split(".")
    for label in labels:
        if not 1 <= len(label) <= 63:
            raise MockError("each dot-separated label must be 1-63 characters long")
        if not _LABEL_RE.match(label):
            raise MockError(f"label {label!r} may only contain a-z, 0-9 and '-', and must not start or end with '-'")
    if _PORT_NAME_RE.match(labels[0]):
        raise MockError("name must not have the form port-xyz or port-xyz-abcde")
    if _IP_LIKE_RE.match(name):
        raise MockError("name must not look like an IP address (n.n.n.n)")
    return name


def check_ipv4(value: Any, what: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(value)
    except (ipaddress.AddressValueError, ValueError, TypeError):
        raise MockError(f"{what} {value!r} is not an IPv4 address") from None


def check_netmask(value: Any) -> ipaddress.IPv4Address:
    mask = check_ipv4(value, "subnet")
    bits = int(mask)
    inverted = ~bits & 0xFFFFFFFF
    if bits == 0 or inverted & (inverted + 1):
        raise MockError(f"subnet {value!r} is not a contiguous netmask")
    return mask


def _int_field(value: Any, what: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MockError(f"{what} must be an integer, got {value!r}")
    if not low <= value <= high:
        raise MockError(f"{what} must be between {low} and {high}, got {value}")
    return value


def build_device(spec: Mapping[str, Any]) -> MockDevice:
    if not isinstance(spec, Mapping):
        raise MockError("each device must be a JSON object")
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise MockError("each device needs a non-empty 'name'")
    name = name.strip()
    try:
        check_station_name(name)  # the same NameOfStation rules set_name enforces
    except MockError as exc:
        raise MockError(f"device {name!r}: {exc}") from None
    ip = str(check_ipv4(spec.get("ip"), f"device {name!r} ip"))
    subnet = str(check_netmask(spec.get("subnet", "255.255.255.0")))
    gateway = str(check_ipv4(spec.get("gateway", "0.0.0.0"), f"device {name!r} gateway"))
    areas: dict[str, IOArea] = {}
    raw_areas = spec.get("areas", [])
    if not isinstance(raw_areas, Sequence) or isinstance(raw_areas, str):
        raise MockError(f"device {name!r}: 'areas' must be a list")
    for raw in raw_areas:
        if not isinstance(raw, Mapping):
            raise MockError(f"device {name!r}: each area must be a JSON object")
        direction = raw.get("direction", "input")
        if direction not in ("input", "output"):
            raise MockError(f"device {name!r}: area direction must be 'input' or 'output'")
        area = IOArea(
            slot=_int_field(raw.get("slot"), "slot", 0, MAX_SLOT),
            subslot=_int_field(raw.get("subslot"), "subslot", 1, MAX_SUBSLOT),
            length=_int_field(raw.get("length"), "length", 1, MAX_AREA_BYTES),
            direction=direction,
        )
        if area.key in areas:
            raise MockError(f"device {name!r}: slot {area.slot}/subslot {area.subslot} is defined twice")
        areas[area.key] = area

    def optional_str(key: str) -> str | None:
        value = spec.get(key)
        return None if value is None else str(value)

    return MockDevice(
        name=name,
        mac=normalize_mac(spec.get("mac")),
        ip=ip,
        subnet=subnet,
        gateway=gateway,
        vendor=optional_str("vendor"),
        vendor_id=optional_str("vendor_id"),
        device_id=optional_str("device_id"),
        device_type=str(spec.get("device_type", "IO Device")),
        type_of_station=optional_str("type_of_station"),
        areas=areas,
    )


def build_devices(specs: Any) -> list[MockDevice]:
    if isinstance(specs, Mapping):
        specs = specs.get("devices")
    if not isinstance(specs, list) or not specs:
        raise MockError("the devices file must hold a non-empty list of devices (or {'devices': [...]})")
    devices = [build_device(spec) for spec in specs]
    names = [d.name.lower() for d in devices]
    macs = [d.mac for d in devices]
    if len(set(names)) != len(names):
        raise MockError("device names must be unique")
    if len(set(macs)) != len(macs):
        raise MockError("device MAC addresses must be unique")
    return devices


def load_devices_file(path: Path) -> list[MockDevice]:
    try:
        specs = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MockError(f"cannot read devices file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise MockError(f"devices file {path} is not valid JSON: {exc}") from exc
    return build_devices(specs)


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


class ProfinetMockServer:
    def __init__(self, config: MockConfig, devices: list[MockDevice] | None = None) -> None:
        self.config = config
        self.devices: dict[str, MockDevice] = {}
        for device in devices if devices is not None else build_devices(DEFAULT_DEVICES):
            self.devices[device.name] = device
        self._server: asyncio.Server | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def port(self) -> int:
        """The bound TCP port (useful when started with port 0)."""
        if not self._server or not self._server.sockets:
            raise RuntimeError("server is not listening")
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        if self._running:
            return
        self._server = await asyncio.start_server(
            self._handle_client, self.config.host, self.config.port, limit=MAX_LINE_BYTES
        )
        self._running = True
        host = self.config.host
        shown = f"[{host}]" if ":" in host else host
        console.print(f"PROFINET mock listening on {shown}:{self.port}", markup=False, highlight=False)
        self._update_task = asyncio.create_task(self._update_loop())

    async def stop(self) -> None:
        self._running = False
        if self._update_task:
            self._update_task.cancel()
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
            self._update_task = None
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _update_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.config.update_interval)
            for device in self.devices.values():
                for area in device.areas.values():
                    if area.direction != "input":
                        continue
                    for idx in range(len(area.buffer)):
                        area.buffer[idx] = (area.buffer[idx] + random.randint(0, 5)) & 0xFF
            if self.config.verbose:
                console.print("[cyan]Updated simulated input values[/cyan]")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        console.print(f"[yellow]Client connected {peer}[/yellow]")
        try:
            while True:
                try:
                    line = await reader.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    await self._send(writer, {"success": False, "error": f"Request longer than {MAX_LINE_BYTES} bytes"})
                    break
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    await self._send(writer, {"success": False, "error": "Invalid JSON"})
                    continue
                response = self.handle_request(request)
                if self.config.verbose:
                    console.print(f"[cyan]{peer} {request!r} -> {response!r}[/cyan]", markup=False)
                await self._send(writer, response)
        except ConnectionError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            console.print(f"[yellow]Client disconnected {peer}[/yellow]")

    def handle_request(self, request: Any) -> dict[str, Any]:
        """Answer one decoded request; never raises."""
        if not isinstance(request, dict):
            return {"success": False, "error": "Request must be a JSON object"}
        op = request.get("op")
        handler = (
            {
                "discover": self._op_discover,
                "read": self._op_read,
                "write": self._op_write,
                "identify": self._op_identify,
                "set_name": self._op_set_name,
                "set_ip": self._op_set_ip,
            }.get(op)
            if isinstance(op, str)
            else None
        )
        if handler is None:
            return {"success": False, "error": f"Unknown op {op!r}"}
        try:
            return {"success": True, "data": handler(request)}
        except MockError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # keep the connection alive whatever happens
            return {"success": False, "error": f"Internal mock error: {exc!r}"}

    # -- lookups -------------------------------------------------------------

    def _device_by_name(self, name: Any) -> MockDevice:
        if not isinstance(name, str) or not name:
            raise MockError("'device' must be a non-empty device name")
        for device in self.devices.values():
            if device.name.lower() == name.lower():
                return device
        raise MockError(f"Device {name!r} not found")

    def _device_by_mac(self, mac: Any) -> MockDevice:
        wanted = normalize_mac(mac)
        for device in self.devices.values():
            if device.mac == wanted:
                return device
        raise MockError(f"No device with MAC {wanted}")

    def _area(self, device: MockDevice, request: Mapping[str, Any]) -> IOArea:
        slot = _int_field(request.get("slot"), "slot", 0, MAX_SLOT)
        subslot = _int_field(request.get("subslot"), "subslot", 1, MAX_SUBSLOT)
        area = device.areas.get(f"{slot}:{subslot}")
        if area is None:
            raise MockError(f"Slot {slot}/subslot {subslot} not found on {device.name}")
        return area

    # -- operations ------------------------------------------------------------

    def _op_discover(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        return [device.describe() for device in self.devices.values()]

    def _op_read(self, request: Mapping[str, Any]) -> dict[str, Any]:
        device = self._device_by_name(request.get("device"))
        area = self._area(device, request)
        length = request.get("length", area.length)
        length = _int_field(length, "length", 1, MAX_AREA_BYTES)
        if length > area.length:
            raise MockError(f"Slot {area.slot}/subslot {area.subslot} holds {area.length} bytes; cannot read {length}")
        return {
            "device_name": device.name,
            "slot": area.slot,
            "subslot": area.subslot,
            "direction": area.direction,
            "raw_data_hex": bytes(area.buffer[:length]).hex(),
        }

    def _op_write(self, request: Mapping[str, Any]) -> dict[str, Any]:
        device = self._device_by_name(request.get("device"))
        area = self._area(device, request)
        if area.direction != "output":
            raise MockError(f"Slot {area.slot}/subslot {area.subslot} is an input area; only outputs can be written")
        data = request.get("data")
        if not isinstance(data, list) or not data:
            raise MockError("'data' must be a non-empty list of byte values")
        payload = bytes(_int_field(b, "each data byte", 0, 255) for b in data)
        if len(payload) > area.length:
            raise MockError(
                f"Slot {area.slot}/subslot {area.subslot} holds {area.length} bytes; cannot write {len(payload)}"
            )
        area.buffer[: len(payload)] = payload
        return {
            "device_name": device.name,
            "slot": area.slot,
            "subslot": area.subslot,
            "written_bytes": len(payload),
        }

    def _op_identify(self, request: Mapping[str, Any]) -> dict[str, Any]:
        device = self._device_by_mac(request.get("mac"))
        duration = _int_field(request.get("duration_s", 5), "duration_s", 1, MAX_IDENTIFY_SECONDS)
        device.identify_until = time.time() + duration
        console.print(f"[magenta]{device.name} ({device.mac}) flashing its LED for {duration}s[/magenta]")
        return {"device_name": device.name, "mac_address": device.mac, "identify_duration_s": duration}

    def _op_set_name(self, request: Mapping[str, Any]) -> dict[str, Any]:
        device = self._device_by_mac(request.get("mac"))
        name = check_station_name(request.get("name"))
        for other in self.devices.values():
            if other is not device and other.name.lower() == name:
                raise MockError(f"Name {name!r} is already used by {other.mac}")
        previous = device.name
        del self.devices[previous]
        device.name = name
        self.devices[name] = device
        return {"mac_address": device.mac, "device_name": name, "previous_name": previous}

    def _op_set_ip(self, request: Mapping[str, Any]) -> dict[str, Any]:
        device = self._device_by_mac(request.get("mac"))
        ip = check_ipv4(request.get("ip"), "ip")
        mask = check_netmask(request.get("subnet"))
        gateway_raw = request.get("gateway") or "0.0.0.0"
        gateway = check_ipv4(gateway_raw, "gateway")
        network = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
        if ip in (network.network_address, network.broadcast_address) and network.prefixlen < 31:
            raise MockError(f"{ip} is the network or broadcast address of {network}")
        if int(gateway) and gateway not in network:
            raise MockError(f"gateway {gateway} is not in {network}")
        device.ip, device.subnet, device.gateway = str(ip), str(mask), str(gateway)
        return {
            "mac_address": device.mac,
            "device_name": device.name,
            "ip_address": device.ip,
            "subnet_mask": device.subnet,
            "gateway": device.gateway,
        }

    async def _send(self, writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        writer.write(json.dumps(message).encode("utf-8") + b"\n")
        await writer.drain()


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> argparse.Namespace:
    try:
        defaults = MockConfig.from_env(environ)
    except ConfigError as exc:
        raise SystemExit(f"profinet-mock-server: {exc}") from None
    parser = argparse.ArgumentParser(
        prog="profinet-mock-server",
        description=(
            "Mock PROFINET IO device. Answers JSON-lines requests over TCP "
            "(discover, read, write, identify, set_name, set_ip); see the README."
        ),
        epilog="Each flag defaults to the matching MOCK_PROFINET_* environment variable (a .env file is honoured).",
    )
    parser.add_argument("--host", default=defaults.host, help="address to listen on (MOCK_PROFINET_HOST)")
    parser.add_argument(
        "--port", type=int, default=defaults.port, help="TCP port, 0 = any free port (MOCK_PROFINET_PORT)"
    )
    parser.add_argument(
        "--update-interval",
        type=float,
        default=defaults.update_interval,
        help="seconds between changes of the simulated inputs (MOCK_PROFINET_UPDATE_INTERVAL)",
    )
    parser.add_argument(
        "--devices",
        type=Path,
        default=defaults.devices_file,
        help="JSON file describing the simulated devices (MOCK_PROFINET_DEVICES_FILE)",
    )
    parser.add_argument(
        "--verbose", action="store_true", default=defaults.verbose, help="log every request (MOCK_PROFINET_VERBOSE)"
    )
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.update_interval <= 0:
        parser.error("--update-interval must be positive")
    return args


async def run_server(config: MockConfig, devices: list[MockDevice] | None = None) -> None:
    server = ProfinetMockServer(config, devices)
    await server.start()

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await stop.wait()
    finally:
        console.print("[red]Stopping PROFINET mock...[/red]")
        await server.stop()


def main(argv: Sequence[str] | None = None) -> None:
    # Settings may come from a .env file in the working directory (or a parent).
    load_dotenv(find_dotenv(usecwd=True))
    args = parse_args(argv)
    config = MockConfig(
        host=args.host,
        port=args.port,
        update_interval=args.update_interval,
        verbose=args.verbose,
        devices_file=args.devices,
    )
    try:
        devices = load_devices_file(config.devices_file) if config.devices_file else None
    except MockError as exc:
        raise SystemExit(f"profinet-mock-server: {exc}") from None
    try:
        asyncio.run(run_server(config, devices))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
