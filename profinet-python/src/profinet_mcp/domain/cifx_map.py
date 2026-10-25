"""Strict, bounded commissioning map for one PROFINET controller process image."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import ConfigError
from ..validation import station_name

MAX_IMAGE_SIZE = 65536


def _object(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ConfigError("Invalid cifX map fields")
    return value


def _integer(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"cifX map integer must be between {low} and {high}")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("Duplicate cifX map key")
        result[key] = value
    return result


@dataclass(frozen=True)
class Mapping:
    device: str
    slot: int
    subslot: int
    ar: int
    direction: str
    offset: int
    length: int
    status_offset: int


@dataclass(frozen=True)
class ProcessMap:
    device_number: int
    serial_number: int
    firmware_name: str
    firmware_version: tuple[int, ...]
    input_size: int
    output_size: int
    bit_list_offset: int
    max_age_ms: int
    mappings: tuple[Mapping, ...]

    @classmethod
    def load(cls, path: Path):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as file:
                if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                    raise ConfigError("cifX map must be a regular file")
                raw = file.read(65537)
            if len(raw) > 65536:
                raise ConfigError("cifX map exceeds 64 KiB")
            data = json.loads(raw, object_pairs_hook=_unique)
            return cls.parse(data)
        except (OSError, ValueError, RecursionError) as exc:
            raise ConfigError(f"Cannot load cifX process map: {type(exc).__name__}") from exc

    @classmethod
    def parse(cls, data):
        fields = {
            "version",
            "device_number",
            "serial_number",
            "firmware_name",
            "firmware_version",
            "input_size",
            "output_size",
            "bit_list_offset",
            "max_age_ms",
            "mappings",
        }
        data = _object(data, fields)
        if type(data["version"]) is not int or data["version"] != 1:
            raise ConfigError("Unsupported cifX process-map version")
        name = data["firmware_name"]
        if not isinstance(name, str) or not 1 <= len(name.encode("ascii")) <= 63 or "\0" in name:
            raise ConfigError("cifX firmware_name must be an exact ASCII firmware identity")
        version = data["firmware_version"]
        if not isinstance(version, list) or len(version) != 4:
            raise ConfigError("cifX firmware_version needs major, minor, build, revision")
        version = tuple(_integer(v, 0, 65535) for v in version)
        if version[:2] != (3, 2):
            raise ConfigError("This adapter implements PROFINET IO Controller V3.2 process-image semantics")
        insize = _integer(data["input_size"], 48, MAX_IMAGE_SIZE)
        outsize = _integer(data["output_size"], 0, MAX_IMAGE_SIZE)
        bitlist = _integer(data["bit_list_offset"], 0, insize - 48)
        age = _integer(data["max_age_ms"], 1, 10000)
        entries = data["mappings"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
            raise ConfigError("cifX map needs 1..256 mappings")
        occupied = {"input": [(bitlist, bitlist + 48)], "output": []}
        keys = set()
        devices = {}
        ars = {}
        parsed = []

        def reserve(direction, start, length):
            size = insize if direction == "input" else outsize
            _integer(start, 0, size - 1)
            if start + length > size or any(
                start < end and start + length > begin for begin, end in occupied[direction]
            ):
                raise ConfigError("Overlapping or out-of-bounds cifX process-image ranges")
            occupied[direction].append((start, start + length))

        for item in entries:
            item = _object(
                item, {"device_name", "slot", "subslot", "ar", "direction", "offset", "length", "status_offset"}
            )
            device = item["device_name"]
            if not isinstance(device, str) or station_name(device) != device:
                raise ConfigError("cifX device_name must be a normalized station name")
            slot = _integer(item["slot"], 0, 65535)
            subslot = _integer(item["subslot"], 1, 65535)
            ar = _integer(item["ar"], 1, 128)
            direction = item["direction"]
            if direction not in ("input", "output"):
                raise ConfigError("cifX direction must be input or output")
            key = (device, slot, subslot, direction)
            if key in keys or devices.get(device, ar) != ar or ars.get(ar, device) != device:
                raise ConfigError("Duplicate mapping or inconsistent device/AR association")
            keys.add(key)
            devices[device] = ar
            ars[ar] = device
            length = _integer(item["length"], 1, 1439)
            offset = _integer(item["offset"], 0, MAX_IMAGE_SIZE - 1)
            status = _integer(item["status_offset"], 0, insize - 1)
            # Input provider status / output consumer status are received from the device.
            reserve(direction, offset, length)
            reserve("input", status, 1)
            if direction == "input" and status != offset + length:
                raise ConfigError("Input IOPS must immediately follow its submodule data")
            if direction == "output":
                # The output provider status is sent atomically with this submodule's data.
                reserve("output", offset + length, 1)
            parsed.append(Mapping(device, slot, subslot, ar, direction, offset, length, status))
        return cls(
            _integer(data["device_number"], 1, 0xFFFFFFFF),
            _integer(data["serial_number"], 1, 0xFFFFFFFF),
            name,
            version,
            insize,
            outsize,
            bitlist,
            age,
            tuple(parsed),
        )

    def resolve(self, device, slot, subslot, direction, length):
        for item in self.mappings:
            if (item.device, item.slot, item.subslot, item.direction) == (device, slot, subslot, direction):
                if item.length != length:
                    raise ConfigError("I/O length must exactly match the commissioned submodule mapping")
                return item
        raise ConfigError("No commissioned cifX mapping for this device, slot, subslot and direction")
