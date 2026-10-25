"""DCP Identify codec and transport, tested against fakes (no raw sockets are opened)."""

from __future__ import annotations

import errno
import struct
import threading

import pytest

from profinet_mcp import dcp

SRC = "02:00:00:00:00:aa"
DEVICE_MAC = "00:1b:1b:12:34:56"
XID = 0x12345678


def block(option: int, suboption: int, value: bytes) -> bytes:
    data = struct.pack("!BBH", option, suboption, len(value)) + value
    return data + (b"\x00" if len(value) % 2 else b"")


def identify_response(
    *,
    xid: int = XID,
    mac: str = DEVICE_MAC,
    name: bytes = b"line1-io",
    vlan: bool = False,
    service_type: int = 1,
    extra: bytes = b"",
) -> bytes:
    """An Identify response written out byte by byte from IEC 61158-6-10."""
    blocks = (
        block(0x02, 0x01, b"\x00\x00" + b"ET 200SP")  # DeviceVendorValue (type of station)
        + block(0x02, 0x02, b"\x00\x00" + name)  # NameOfStation
        + block(0x02, 0x03, b"\x00\x00" + struct.pack("!HH", 0x002A, 0x0313))  # VendorID / DeviceID
        + block(0x02, 0x04, b"\x00\x00\x01\x00")  # DeviceRole: IO device
        + block(0x01, 0x02, b"\x00\x01" + bytes([192, 168, 1, 50, 255, 255, 255, 0, 192, 168, 1, 1]))  # IP set
        + block(0x02, 0x07, b"\x00\x00\x00\x01")  # DeviceInstance 0.1
        + extra
    )
    header = struct.pack("!BBIHH", 0x05, service_type, xid, 0, len(blocks))
    ether = dcp.mac_to_bytes(SRC) + dcp.mac_to_bytes(mac)
    if vlan:
        ether += struct.pack("!HH", 0x8100, 0x6000)
    return ether + struct.pack("!HH", 0x8892, 0xFEFF) + header + blocks


# -- encoding ------------------------------------------------------------------


def test_identify_all_request_bytes() -> None:
    frame = dcp.build_identify_all_request(SRC, XID)
    assert len(frame) == 60
    assert frame[0:6] == bytes.fromhex("010ecf000000")  # DCP Identify multicast
    assert frame[6:12] == bytes.fromhex("0200000000aa")
    assert frame[12:16] == bytes.fromhex("8892fefe")  # PROFINET ethertype, Identify request FrameID
    assert frame[16:26] == bytes.fromhex("05001234567800010004")  # Identify/request, Xid, delay, length
    assert frame[26:30] == bytes.fromhex("ffff0000")  # AllSelector block
    assert frame[30:] == b"\x00" * 30


def test_request_dissects_with_scapy_contrib_layer() -> None:
    """Cross-check against an independent implementation (Scapy's PROFINET DCP dissector)."""
    from scapy.contrib.pnio import ProfinetIO
    from scapy.contrib.pnio_dcp import ProfinetDCP
    from scapy.layers.l2 import Ether

    pkt = Ether(dcp.build_identify_all_request(SRC, XID))
    assert pkt[ProfinetIO].frameID == 0xFEFE
    layer = pkt[ProfinetDCP]
    assert (layer.service_id, layer.service_type, layer.xid) == (5, 0, XID)
    assert (layer.option, layer.sub_option, layer.dcp_data_length) == (0xFF, 0xFF, 4)


# -- decoding ------------------------------------------------------------------


def test_parse_identify_response() -> None:
    record = dcp.parse_identify_response(identify_response(), XID)
    assert record == {
        "device_name": "line1-io",
        "mac_address": DEVICE_MAC,
        "ip_address": "192.168.1.50",
        "subnet_mask": "255.255.255.0",
        "gateway": "192.168.1.1",
        "ip_status": "set",
        "vendor": None,
        "vendor_id": "0x002A",
        "device_id": "0x0313",
        "device_type": "IO Device",
        "device_roles": ["IO Device"],
        "type_of_station": "ET 200SP",
        "alias_name": None,
        "device_instance": "0.1",
    }


def test_parse_response_built_by_scapy() -> None:
    from scapy.contrib.pnio import ProfinetIO
    from scapy.contrib.pnio_dcp import (
        DCPDeviceIDBlock,
        DCPIPBlock,
        DCPNameOfStationBlock,
        ProfinetDCP,
    )
    from scapy.layers.l2 import Ether

    pkt = (
        Ether(src=DEVICE_MAC, dst=SRC, type=0x8892)
        / ProfinetIO(frameID=0xFEFF)
        / ProfinetDCP(service_id=5, service_type=1, xid=XID)
        / DCPNameOfStationBlock(name_of_station=b"press-7")
        # Scapy's contrib builder gets these two blocks wrong when left automatic (dcp_block_length 11
        # and 0, plus a pad byte after even-length blocks), so length and padding are set explicitly.
        / DCPIPBlock(dcp_block_length=14, ip="10.1.2.3", netmask="255.255.0.0", gateway="10.1.0.1", padding=b"")
        / DCPDeviceIDBlock(dcp_block_length=6, vendor_id=0x00B0, device_id=0x0102, padding=b"")
    )
    record = dcp.parse_identify_response(bytes(pkt), XID)
    assert record is not None
    assert record["device_name"] == "press-7"
    assert (record["ip_address"], record["subnet_mask"], record["gateway"]) == ("10.1.2.3", "255.255.0.0", "10.1.0.1")
    assert (record["vendor_id"], record["device_id"]) == ("0x00B0", "0x0102")


def test_hand_written_fixture_dissects_with_scapy() -> None:
    from scapy.contrib.pnio_dcp import DCPDeviceIDBlock, DCPIPBlock, DCPNameOfStationBlock, ProfinetDCP
    from scapy.layers.l2 import Ether

    pkt = Ether(identify_response())
    assert pkt[ProfinetDCP].xid == XID
    assert pkt[DCPNameOfStationBlock].name_of_station == b"line1-io"
    assert pkt[DCPIPBlock].ip == "192.168.1.50"
    assert pkt[DCPDeviceIDBlock].vendor_id == 0x002A


def test_vlan_tagged_response() -> None:
    record = dcp.parse_identify_response(identify_response(vlan=True), XID)
    assert record is not None and record["device_name"] == "line1-io"


@pytest.mark.parametrize(
    "frame",
    [
        identify_response(xid=XID + 1),  # someone else's request
        identify_response(service_type=5),  # "request not supported"
        identify_response()[:-6],  # truncated block
        identify_response()[:13],  # not even an Ethernet header
        dcp.build_identify_all_request(SRC, XID),  # our own request echoed back
        bytes(12) + b"\x08\x00" + bytes(46),  # IPv4
    ],
)
def test_frames_that_are_not_valid_answers_are_ignored(frame: bytes) -> None:
    assert dcp.parse_identify_response(frame, XID) is None


def test_odd_length_names_are_padded_and_unknown_blocks_skipped() -> None:
    frame = identify_response(name=b"abc", extra=block(0x02, 0x05, b"\x00\x00\x02\x02\x02\x03"))
    record = dcp.parse_identify_response(frame, XID)
    assert record is not None and record["device_name"] == "abc"


def test_ip_conflict_flag() -> None:
    conflict = block(0x01, 0x02, b"\x00\x81" + bytes(12))
    frame = identify_response(extra=conflict)
    assert dcp.parse_identify_response(frame, XID)["ip_status"] == "set (address conflict detected)"


# -- identify_all over a fake transport -------------------------------------------


class FakeTransport:
    interface = "fake0"

    def __init__(self, replies: list[bytes]) -> None:
        self.replies = replies
        self.sent: list[bytes] = []

    def local_mac(self) -> str:
        return SRC

    def exchange(self, frame, timeout, accept):
        self.sent.append(frame)
        return [reply for reply in self.replies if accept(reply)]


def test_identify_all_collects_and_deduplicates() -> None:
    second = identify_response(mac="00:1b:1b:00:00:02", name=b"line2-io")
    transport = FakeTransport([identify_response(), identify_response(), second, identify_response(xid=1)])
    devices, info = dcp.identify_all(transport, timeout=0.1, xid=XID)
    assert [d["device_name"] for d in devices] == ["line1-io", "line2-io"]
    assert info["frames_received"] == 3  # the foreign Xid was filtered out
    assert info["interface"] == "fake0"
    assert transport.sent == [dcp.build_identify_all_request(SRC, XID)]


def test_identify_all_with_no_answers() -> None:
    devices, info = dcp.identify_all(FakeTransport([]), timeout=0.1, xid=XID)
    assert devices == [] and info["frames_received"] == 0


# -- ScapyL2Transport wiring with a fake Scapy API ---------------------------------


class FakePacket:
    def __init__(self, data: bytes) -> None:
        self.original = data


class FakeSniffer:
    fail_with: BaseException | None = None
    captured: list[bytes] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.running = False
        self.exception = None
        self.thread = None

    def start(self) -> None:
        if self.fail_with is not None:
            self.exception = self.fail_with
            self.thread = threading.Thread(target=lambda: None)
            self.thread.start()
            return
        self.running = True
        self.kwargs["started_callback"]()

    def stop(self):
        self.running = False
        lfilter = self.kwargs["lfilter"]
        packets = [FakePacket(data) for data in self.captured if lfilter(FakePacket(data))]
        for packet in packets:
            self.kwargs["prn"](packet)
        return packets if self.kwargs["store"] else []


class FakeScapy:
    def __init__(self, sniffer_cls=FakeSniffer, send_error: BaseException | None = None, mac=SRC) -> None:
        self.AsyncSniffer = sniffer_cls
        self.sent: list[tuple[bytes, str]] = []
        self.send_error = send_error
        self.mac = mac

    @staticmethod
    def Raw(data: bytes) -> bytes:  # noqa: N802 - mirrors scapy.packet.Raw
        return data

    def sendp(self, packet, iface, verbose) -> None:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((packet, iface))

    def get_if_hwaddr(self, iface: str) -> str:
        return self.mac


def test_scapy_transport_sends_on_the_interface_and_filters_capture(monkeypatch) -> None:
    monkeypatch.setattr(dcp, "SNIFFER_START_TIMEOUT", 0.5)

    class Sniffer(FakeSniffer):
        captured = [identify_response(), bytes(60)]

    api = FakeScapy(sniffer_cls=Sniffer)
    transport = dcp.ScapyL2Transport("eth9", scapy=api)
    devices, info = dcp.identify_all(transport, timeout=0.01, xid=XID)
    assert api.sent == [(dcp.build_identify_all_request(SRC, XID), "eth9")]
    assert [d["device_name"] for d in devices] == ["line1-io"]
    assert info["frames_received"] == 1


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(errno.EPERM, "Operation not permitted"),
        OSError(errno.EACCES, "Permission denied: could not open /dev/bpf0"),
    ],
)
def test_capture_permission_errors_are_explained(monkeypatch, error) -> None:
    monkeypatch.setattr(dcp, "SNIFFER_START_TIMEOUT", 0.05)

    class Sniffer(FakeSniffer):
        fail_with = error

    transport = dcp.ScapyL2Transport("eth0", scapy=FakeScapy(sniffer_cls=Sniffer))
    with pytest.raises(dcp.DcpPermissionError, match="CAP_NET_RAW") as caught:
        dcp.identify_all(transport, timeout=0.01, xid=XID)
    assert caught.value.request_sent is False


def test_send_failure_is_reported(monkeypatch) -> None:
    transport = dcp.ScapyL2Transport("eth0", scapy=FakeScapy(send_error=OSError(errno.ENETDOWN, "Network is down")))
    with pytest.raises(dcp.DcpError, match="Network is down") as caught:
        dcp.identify_all(transport, timeout=0.01, xid=XID)
    assert caught.value.request_sent is None


def test_capture_failure_after_send_retains_send_evidence():
    class Sniffer(FakeSniffer):
        def stop(self):
            raise OSError("capture failed")

    api = FakeScapy(sniffer_cls=Sniffer)
    transport = dcp.ScapyL2Transport("eth0", scapy=api)
    with pytest.raises(dcp.DcpError, match="capture failed") as caught:
        dcp.identify_all(transport, timeout=0.01, xid=XID)
    assert caught.value.request_sent is True
    assert len(api.sent) == 1


def test_unknown_interface_is_reported() -> None:
    transport = dcp.ScapyL2Transport("nope0", scapy=FakeScapy(mac="00:00:00:00:00:00"))
    with pytest.raises(dcp.DcpError, match="PROFINET_INTERFACE"):
        dcp.identify_all(transport, timeout=0.01)


def test_scapy_api_names_exist() -> None:
    """The real transport imports these lazily; make sure they exist in the locked Scapy (import only)."""
    api = dcp._load_scapy()
    assert callable(api.AsyncSniffer) and callable(api.sendp) and callable(api.get_if_hwaddr)


def test_capture_limits_are_visible_in_discovery(monkeypatch):
    monkeypatch.setattr(dcp, "MAX_CAPTURE_FRAMES", 2)

    class Sniffer(FakeSniffer):
        captured = [identify_response()] * 5

    transport = dcp.ScapyL2Transport("fake0", scapy=FakeScapy(sniffer_cls=Sniffer))
    devices, info = dcp.identify_all(transport, timeout=0, xid=XID)
    assert len(devices) == 1
    assert info["frames_received"] == 2
    assert info["capture_truncated"] is True and info["complete"] is False


def test_capture_byte_limit_is_independent_of_count(monkeypatch):
    monkeypatch.setattr(dcp, "MAX_CAPTURE_BYTES", len(identify_response()) - 1)

    class Sniffer(FakeSniffer):
        captured = [identify_response()]

    transport = dcp.ScapyL2Transport("fake0", scapy=FakeScapy(sniffer_cls=Sniffer))
    devices, info = dcp.identify_all(transport, timeout=0, xid=XID)
    assert devices == [] and info["capture_truncated"] is True
