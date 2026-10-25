"""Single-block unicast DCP Set; a missing usable acknowledgement is uncertain."""

from __future__ import annotations

import secrets
import struct
from collections.abc import Callable

from . import dcp

FRAME_ID = 0xFEFD
SERVICE_ID = 4
HEADER = struct.Struct("!BBIHH")
BLOCK = struct.Struct("!BBH")


class SetRejected(dcp.DcpError):
    """A correlated protocol response explicitly refused the only requested block."""


def unicast_mac(value: str) -> bytes:
    raw = dcp.mac_to_bytes(value)
    if raw[0] & 1 or raw == bytes(6):
        raise ValueError("DCP Set requires an explicit nonzero unicast MAC address")
    return raw


def build_request(
    source: str, target: str, xid: int, option: int, suboption: int, value: bytes, qualifier: int
) -> bytes:
    payload = struct.pack("!H", qualifier) + value
    block = BLOCK.pack(option, suboption, len(payload)) + payload
    if len(payload) % 2:
        block += b"\x00"
    header = HEADER.pack(SERVICE_ID, 0, xid, 0, len(block))
    return (
        unicast_mac(target)
        + unicast_mac(source)
        + struct.pack("!HH", dcp.ETHERTYPE_PROFINET, FRAME_ID)
        + header
        + block
    ).ljust(dcp.MIN_FRAME_LENGTH, b"\x00")


def response_filter(source: str, target: str, xid: int) -> Callable[[bytes], bool]:
    local, remote = unicast_mac(source), unicast_mac(target)

    def accepts(frame: bytes) -> bool:
        offset = dcp._pdu_offset(frame)
        if offset is None or len(frame) < offset + 2 + HEADER.size:
            return False
        if frame[:6] != local or frame[6:12] != remote or struct.unpack_from("!H", frame, offset)[0] != FRAME_ID:
            return False
        service, kind, rxid, _, _ = HEADER.unpack_from(frame, offset + 2)
        return service == SERVICE_ID and kind in (1, 5) and rxid == xid

    return accepts


def response_status(frame: bytes, option: int, suboption: int) -> int:
    offset = dcp._pdu_offset(frame)
    assert offset is not None
    _, kind, _, _, length = HEADER.unpack_from(frame, offset + 2)
    start = offset + 2 + HEADER.size
    payload = frame[start : start + length]
    if len(payload) != length:
        raise ValueError("Truncated DCP Set response")
    if kind == 5:
        if length:
            raise ValueError("Malformed unsupported-service response")
        return 255
    # Exactly one Control/Response block, echoing the one option/suboption sent.
    if len(payload) != 8 or BLOCK.unpack_from(payload) != (5, 4, 3):
        raise ValueError("DCP Set response has no single complete Control/Response block")
    if tuple(payload[4:6]) != (option, suboption) or payload[7] != 0:
        raise ValueError("DCP Set response does not acknowledge the requested block")
    if payload[6] > 6:
        raise ValueError("DCP Set response carries a reserved block error")
    return payload[6]


def set_value(transport, timeout: float, target: str, option: int, suboption: int, value: bytes, *, qualifier: int = 1):
    xid = secrets.randbits(32)
    try:
        source = transport.local_mac()
        request = build_request(source, target, xid, option, suboption, value, qualifier)
        accept = response_filter(source, target, xid)
    except (ValueError, dcp.DcpError) as exc:
        raise dcp.DcpError(str(exc), request_sent=False) from exc
    frames = transport.exchange(request, timeout, accept)
    try:
        statuses = {response_status(frame, option, suboption) for frame in frames if accept(frame)}
    except ValueError as exc:
        raise dcp.DcpError(f"DCP Set outcome unknown: {exc}", request_sent=True) from exc
    if getattr(frames, "truncated", False) or len(statuses) != 1:
        raise dcp.DcpError(
            "DCP Set outcome unknown: no unambiguous acknowledgement; do not automatically retry", request_sent=True
        )
    status = statuses.pop()
    if status:
        raise SetRejected(f"The device rejected DCP Set (block error 0x{status:02x})", request_sent=True)
    return {
        "protocol": "DCP Set",
        "xid": f"0x{xid:08x}",
        "request_sent": True,
        "confirmation": "protocol_acknowledgement",
        "physical_completion_verified": False,
    }
