"""PROFINET DCP Identify: the read-only part of the Discovery and basic Configuration Protocol.

The network backend multicasts one *Identify All* request on the configured
interface and decodes the *Identify* responses (IEC 61158-6-10). DCP *Set*
(station name, IP suite, identification signal) is deliberately not
implemented here: it changes devices and has not been verified against
hardware.

DCP frames are small TLV structures, encoded and decoded below with
``struct``. Sending and capturing raw Ethernet frames is done by Scapy through
:class:`ScapyL2Transport`, which needs raw-socket privileges (root or
``CAP_NET_RAW`` on Linux, access to ``/dev/bpf*`` on macOS). Everything that
touches the wire sits behind the :class:`L2Transport` protocol so it can be
replaced by a fake in tests.
"""

from __future__ import annotations

import errno
import random
import struct
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, Protocol

ETHERTYPE_PROFINET = 0x8892
VLAN_ETHERTYPES = frozenset({0x8100, 0x88A8})
DCP_MULTICAST_MAC = "01:0e:cf:00:00:00"
FRAME_ID_IDENTIFY_REQUEST = 0xFEFE
FRAME_ID_IDENTIFY_RESPONSE = 0xFEFF
SERVICE_ID_IDENTIFY = 0x05
SERVICE_TYPE_REQUEST = 0x00
SERVICE_TYPE_RESPONSE_SUCCESS = 0x01
OPTION_ALL_SELECTOR = 0xFF
SUBOPTION_ALL_SELECTOR = 0xFF
#: ResponseDelayFactor 1 = "answer without spreading the response".
RESPONSE_DELAY_NO_SPREAD = 0x0001
MIN_FRAME_LENGTH = 60  # Ethernet minimum without FCS
SNIFFER_START_TIMEOUT = 3.0

_DCP_HEADER = struct.Struct("!BBIHH")  # ServiceID, ServiceType, Xid, ResponseDelay/Reserved, DCPDataLength
_BLOCK_HEADER = struct.Struct("!BBH")  # Option, Suboption, DCPBlockLength

DEVICE_ROLE_BITS = (
    (0x01, "IO Device"),
    (0x02, "IO Controller"),
    (0x04, "IO Multidevice"),
    (0x08, "PN Supervisor"),
)
IP_STATUS = {0: "not set", 1: "set", 2: "set by DHCP"}


class DcpError(RuntimeError):
    """DCP could not be carried out on the interface."""


class DcpPermissionError(DcpError):
    """The process may not send/capture raw Ethernet frames on the interface."""


class L2Transport(Protocol):
    """Raw layer-2 access used by :func:`identify_all`."""

    interface: str

    def local_mac(self) -> str:
        """MAC address of ``interface`` (used as the request's source address)."""

    def exchange(self, frame: bytes, timeout: float, accept: Callable[[bytes], bool]) -> list[bytes]:
        """Send ``frame`` and return every received frame ``accept`` likes, for ``timeout`` seconds."""


# ---------------------------------------------------------------------------
# Encoding / decoding
# ---------------------------------------------------------------------------


def mac_to_bytes(mac: str) -> bytes:
    parts = mac.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return bytes(int(part, 16) for part in parts)


def bytes_to_mac(raw: bytes) -> str:
    return ":".join(f"{b:02x}" for b in raw)


def build_identify_all_request(source_mac: str, xid: int, response_delay: int = RESPONSE_DELAY_NO_SPREAD) -> bytes:
    """Ethernet frame for a DCP Identify request with the AllSelector block."""
    block = _BLOCK_HEADER.pack(OPTION_ALL_SELECTOR, SUBOPTION_ALL_SELECTOR, 0)
    dcp = _DCP_HEADER.pack(SERVICE_ID_IDENTIFY, SERVICE_TYPE_REQUEST, xid & 0xFFFFFFFF, response_delay, len(block))
    frame = (
        mac_to_bytes(DCP_MULTICAST_MAC)
        + mac_to_bytes(source_mac)
        + struct.pack("!HH", ETHERTYPE_PROFINET, FRAME_ID_IDENTIFY_REQUEST)
        + dcp
        + block
    )
    return frame.ljust(MIN_FRAME_LENGTH, b"\x00")


def _pdu_offset(frame: bytes) -> int | None:
    """Offset of the FrameID in a PROFINET frame (skipping VLAN tags), else None."""
    if len(frame) < 14:
        return None
    offset = 12
    ethertype = struct.unpack_from("!H", frame, offset)[0]
    while ethertype in VLAN_ETHERTYPES:
        offset += 4
        if len(frame) < offset + 2:
            return None
        ethertype = struct.unpack_from("!H", frame, offset)[0]
    if ethertype != ETHERTYPE_PROFINET:
        return None
    return offset + 2


def is_identify_response(frame: bytes, xid: int | None = None) -> bool:
    """Cheap pre-filter: PROFINET ethertype, Identify response FrameID and (optionally) our Xid."""
    offset = _pdu_offset(frame)
    if offset is None or len(frame) < offset + 2 + _DCP_HEADER.size:
        return False
    if struct.unpack_from("!H", frame, offset)[0] != FRAME_ID_IDENTIFY_RESPONSE:
        return False
    service_id, _service_type, rxid, _reserved, _length = _DCP_HEADER.unpack_from(frame, offset + 2)
    return service_id == SERVICE_ID_IDENTIFY and (xid is None or rxid == xid)


def _text(value: bytes) -> str | None:
    text = value.decode("ascii", errors="replace").rstrip("\x00")
    return text or None


def parse_identify_response(frame: bytes, xid: int | None = None) -> dict[str, Any] | None:
    """Decode one DCP Identify response into a device record; None if it is not one (or is malformed)."""
    if not is_identify_response(frame, xid):
        return None
    offset = _pdu_offset(frame)
    assert offset is not None
    service_id, service_type, rxid, _reserved, data_length = _DCP_HEADER.unpack_from(frame, offset + 2)
    if service_type != SERVICE_TYPE_RESPONSE_SUCCESS:
        return None
    start = offset + 2 + _DCP_HEADER.size
    data = frame[start : start + data_length]
    if len(data) < data_length:
        return None  # truncated frame

    record: dict[str, Any] = {
        "device_name": None,
        "mac_address": bytes_to_mac(frame[6:12]),
        "ip_address": None,
        "subnet_mask": None,
        "gateway": None,
        "ip_status": None,
        "vendor": None,  # DCP only carries the numeric vendor ID
        "vendor_id": None,
        "device_id": None,
        "device_type": None,
        "device_roles": [],
        "type_of_station": None,
        "alias_name": None,
        "device_instance": None,
    }
    pos = 0
    while pos + _BLOCK_HEADER.size <= len(data):
        option, suboption, block_length = _BLOCK_HEADER.unpack_from(data, pos)
        value = data[pos + _BLOCK_HEADER.size : pos + _BLOCK_HEADER.size + block_length]
        if len(value) < block_length:
            return None  # block runs past the end of the PDU
        pos += _BLOCK_HEADER.size + block_length + (block_length % 2)
        if block_length < 2:
            continue
        info, body = struct.unpack_from("!H", value)[0], value[2:]
        if (option, suboption) == (0x01, 0x02) and len(body) >= 12:
            record["ip_address"] = ".".join(str(b) for b in body[0:4])
            record["subnet_mask"] = ".".join(str(b) for b in body[4:8])
            record["gateway"] = ".".join(str(b) for b in body[8:12])
            status = IP_STATUS.get(info & 0x7F, f"reserved (0x{info:04x})")
            record["ip_status"] = status + (" (address conflict detected)" if info & 0x80 else "")
        elif (option, suboption) == (0x02, 0x01):
            record["type_of_station"] = _text(body)
        elif (option, suboption) == (0x02, 0x02):
            record["device_name"] = _text(body)
        elif (option, suboption) == (0x02, 0x03) and len(body) >= 4:
            vendor_id, device_id = struct.unpack_from("!HH", body)
            record["vendor_id"] = f"0x{vendor_id:04X}"
            record["device_id"] = f"0x{device_id:04X}"
        elif (option, suboption) == (0x02, 0x04) and len(body) >= 1:
            roles = [name for bit, name in DEVICE_ROLE_BITS if body[0] & bit]
            record["device_roles"] = roles
            record["device_type"] = ", ".join(roles) or None
        elif (option, suboption) == (0x02, 0x06):
            record["alias_name"] = _text(body)
        elif (option, suboption) == (0x02, 0x07) and len(body) >= 2:
            record["device_instance"] = f"{body[0]}.{body[1]}"
    return record


def identify_all(transport: L2Transport, timeout: float, xid: int | None = None) -> tuple[list[dict[str, Any]], dict]:
    """Multicast DCP Identify All and collect the answers for ``timeout`` seconds (blocking)."""
    if xid is None:
        xid = random.getrandbits(32)
    request = build_identify_all_request(transport.local_mac(), xid)
    frames = transport.exchange(request, timeout, lambda frame: is_identify_response(frame, xid))
    devices: dict[str, dict[str, Any]] = {}
    malformed = 0
    for frame in frames:
        record = parse_identify_response(frame, xid)
        if record is None:
            malformed += 1
            continue
        devices.setdefault(record["mac_address"], record)
    info = {
        "protocol": "DCP Identify All",
        "interface": transport.interface,
        "xid": f"0x{xid:08x}",
        "frames_received": len(frames),
        "malformed_frames": malformed,
    }
    return list(devices.values()), info


# ---------------------------------------------------------------------------
# Scapy transport
# ---------------------------------------------------------------------------


def _load_scapy() -> SimpleNamespace:
    try:
        from scapy.arch import get_if_hwaddr
        from scapy.packet import Raw
        from scapy.sendrecv import AsyncSniffer, sendp
    except ImportError as exc:  # pragma: no cover - scapy is a hard dependency
        raise DcpError("scapy is required for the network backend (scapy>=2.5)") from exc
    return SimpleNamespace(AsyncSniffer=AsyncSniffer, sendp=sendp, Raw=Raw, get_if_hwaddr=get_if_hwaddr)


def _wrap_os_error(interface: str, exc: BaseException) -> DcpError:
    text = str(exc)
    denied = isinstance(exc, PermissionError) or (isinstance(exc, OSError) and exc.errno in (errno.EPERM, errno.EACCES))
    if denied or "permission" in text.lower() or "operation not permitted" in text.lower():
        return DcpPermissionError(
            f"Raw Ethernet access on interface {interface!r} was refused ({text}). DCP discovery needs "
            "root or CAP_NET_RAW on Linux, or read/write access to /dev/bpf* on macOS."
        )
    return DcpError(f"DCP on interface {interface!r} failed: {text or type(exc).__name__}")


class ScapyL2Transport:
    """Send one frame and capture the answers with Scapy (needs raw-socket privileges)."""

    def __init__(self, interface: str, scapy: Any | None = None) -> None:
        self.interface = interface
        self._scapy = scapy

    def _api(self) -> Any:
        if self._scapy is None:
            self._scapy = _load_scapy()
        return self._scapy

    def local_mac(self) -> str:
        try:
            mac = self._api().get_if_hwaddr(self.interface)
        except DcpError:
            raise
        except Exception as exc:
            raise DcpError(f"Cannot read the MAC address of interface {self.interface!r}: {exc}") from exc
        if not mac or mac == "00:00:00:00:00:00":
            raise DcpError(
                f"Interface {self.interface!r} was not found or has no MAC address; set PROFINET_INTERFACE "
                "to the Ethernet interface on the PROFINET network"
            )
        return mac

    def exchange(self, frame: bytes, timeout: float, accept: Callable[[bytes], bool]) -> list[bytes]:
        api = self._api()
        started = threading.Event()

        def raw_bytes(packet: Any) -> bytes:
            original = getattr(packet, "original", None)
            return bytes(original) if original else bytes(packet)

        sniffer = api.AsyncSniffer(
            iface=self.interface,
            store=True,
            lfilter=lambda packet: accept(raw_bytes(packet)),
            started_callback=started.set,
        )
        sniffer.start()
        if not started.wait(SNIFFER_START_TIMEOUT):
            thread = getattr(sniffer, "thread", None)
            if thread is not None:
                thread.join(1.0)
            failure = getattr(sniffer, "exception", None)
            if getattr(sniffer, "running", False):
                self._stop(sniffer)
            if failure is not None:
                raise _wrap_os_error(self.interface, failure) from failure
            raise DcpError(f"Packet capture on interface {self.interface!r} did not start")
        try:
            api.sendp(api.Raw(frame), iface=self.interface, verbose=False)
        except Exception as exc:
            self._stop(sniffer)
            raise _wrap_os_error(self.interface, exc) from exc
        time.sleep(timeout)
        try:
            packets = sniffer.stop()
        except Exception as exc:
            raise _wrap_os_error(self.interface, exc) from exc
        return [raw_bytes(packet) for packet in packets or []]

    @staticmethod
    def _stop(sniffer: Any) -> None:
        try:
            sniffer.stop()
        except Exception:  # already failed; the caller reports the original error
            pass
