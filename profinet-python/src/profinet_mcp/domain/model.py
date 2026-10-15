from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field, ValidationError

from profinet_mcp import validation

log = logging.getLogger(__name__)
Envelope = dict[str, Any]
DeviceName = Annotated[
    str, Field(min_length=1, max_length=240, description="Station name (NameOfStation) of the device, e.g. line1-io")
]
DeviceRef = Annotated[
    str, Field(min_length=1, max_length=240, description="Station name, IP address or MAC address of the device")
]
DeviceMac = Annotated[str, Field(min_length=1, max_length=64, description="MAC address, e.g. 00:11:22:33:44:55")]
Slot = Annotated[
    int, Field(strict=True, ge=0, le=validation.MAX_SLOT, description="Slot number (slot 0 is the device access point)")
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
        strict=True, ge=0.5, le=60, description="Seconds to wait for answers, 0.5-60 (default: PROFINET_DCP_TIMEOUT, 5)"
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


def envelope(success: bool, data: Any = None, error: str | None = None, meta: dict[str, Any] | None = None) -> Envelope:
    return {"success": success, "data": data, "error": error, "meta": meta or {}}


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
