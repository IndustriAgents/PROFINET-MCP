"""Native DCP configuration against packet fixtures, never a physical interface."""

import struct
import threading

import anyio
import pytest
from scapy.contrib.pnio import ProfinetIO
from scapy.contrib.pnio_dcp import DCPControlBlock, ProfinetDCP
from scapy.layers.l2 import Ether
from test_dcp import SRC, FakeScapy, FakeSniffer
from test_tools import MAC, call, make_server

from profinet_mcp import dcp
from profinet_mcp.backends import NetworkBackend, RateLimited, tool_call
from profinet_mcp.domain.capabilities import capabilities

pytestmark = pytest.mark.anyio


class SetTransport:
    interface = "fake0"

    def __init__(self, change=None):
        self.sent = []
        self.change = change

    def local_mac(self):
        return SRC

    def exchange(self, frame, timeout, accept):
        self.sent.append(frame)
        request = Ether(frame)[ProfinetDCP]
        response = bytes(
            Ether(src=MAC, dst=SRC, type=0x8892)
            / ProfinetIO(frameID=0xFEFD)
            / ProfinetDCP(service_id=4, service_type=1, xid=request.xid)
            / DCPControlBlock(response=request.option, response_sub_option=request.sub_option)
        )
        replies = [response] if self.change is None else self.change(response)
        return [item for item in replies if accept(item)]


@pytest.mark.parametrize(
    "tool,args,option,suboption",
    [
        ("set_device_name", {"name": "line-123"}, 2, 2),
        ("set_device_ip", {"ip_address": "10.1.2.3", "subnet_mask": "255.255.255.0"}, 1, 2),
        ("identify_device", {}, 5, 3),
    ],
)
async def test_native_set_round_trip_uses_one_correlated_frame(tool, args, option, suboption):
    transport = SetTransport()
    backend = NetworkBackend("fake0", transport=transport)
    server, _ = make_server(backend, writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, tool, device_mac=MAC, **args)
    assert result["success"], result
    assert result["meta"]["confirmation"] == "protocol_acknowledgement"
    assert result["meta"]["physical_completion_verified"] is False
    assert result["meta"]["request_sent"] is True
    assert len(transport.sent) == 1
    packet = Ether(transport.sent[0])
    assert packet.dst == MAC and packet.src == SRC
    assert packet[ProfinetIO].frameID == 0xFEFD
    request = packet[ProfinetDCP]
    assert (request.service_id, request.service_type, request.option, request.sub_option) == (4, 0, option, suboption)
    if tool == "set_device_name":
        assert request.name_of_station == b"line-123" and request.block_qualifier == 1
    elif tool == "set_device_ip":
        assert request.ip == "10.1.2.3" and request.block_qualifier == 1
    else:
        assert transport.sent[0][26:34] == bytes.fromhex("0503000400000100")


def mutate(raw, offset, data):
    return raw[:offset] + data + raw[offset + len(data) :]


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: [],
        lambda raw: [mutate(raw, 6, bytes.fromhex("001122334466"))],  # other device
        lambda raw: [mutate(raw, 0, bytes.fromhex("0200000000bb"))],  # other destination
        lambda raw: [mutate(raw, 18, bytes(4))],  # stale transaction
        lambda raw: [mutate(raw, 16, bytes([3]))],  # wrong service
        lambda raw: [mutate(raw, 31, bytes([3]))],  # wrong acknowledged suboption
        lambda raw: [mutate(raw, 32, bytes([99]))],  # reserved block error
        lambda raw: [mutate(raw, 24, struct.pack("!H", 9))],  # truncated response
        lambda raw: [raw, mutate(raw, 32, bytes([4]))],  # conflicting statuses
    ],
)
async def test_missing_or_malformed_acknowledgement_is_unknown_without_resend(change):
    transport = SetTransport(change)
    server, _ = make_server(NetworkBackend("fake0", transport=transport), writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, "set_device_name", device_mac=MAC, name="new-name")
    assert not result["success"] and result["meta"]["outcome"] == "unknown"
    assert len(transport.sent) == 1


async def test_correlated_block_error_is_a_rejection():
    transport = SetTransport(lambda raw: [mutate(raw, 32, bytes([6]))])
    server, _ = make_server(NetworkBackend("fake0", transport=transport), writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, "set_device_name", device_mac=MAC, name="new-name")
    assert result["meta"]["outcome"] == "rejected" and len(transport.sent) == 1


async def test_capture_loss_cannot_establish_set_success():
    class Truncated(SetTransport):
        def exchange(self, frame, timeout, accept):
            frames = dcp.CapturedFrames(super().exchange(frame, timeout, accept))
            frames.truncated = True
            return frames

    transport = Truncated()
    server, _ = make_server(NetworkBackend("fake0", transport=transport), writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, "set_device_name", device_mac=MAC, name="new-name")
    assert not result["success"] and result["meta"]["outcome"] == "unknown"
    assert len(transport.sent) == 1


async def test_native_send_error_does_not_invent_confirmed_send():
    api = FakeScapy(send_error=OSError("send failure"))
    transport = dcp.ScapyL2Transport("fake0", scapy=api)
    server, _ = make_server(NetworkBackend("fake0", transport=transport), writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, "set_device_name", device_mac=MAC, name="new-name")
    assert result["meta"]["outcome"] == "unknown" and result["meta"]["request_sent"] is None


async def test_native_configuration_still_requires_both_gates():
    for gates in ({}, {"writes_enabled": True}, {"config_cmds_enabled": True}):
        transport = SetTransport()
        server, _ = make_server(NetworkBackend("fake0", transport=transport), **gates)
        result = await call(server, "set_device_name", device_mac=MAC, name="new-name")
        assert result["meta"]["outcome"] == "not_sent" and transport.sent == []


@pytest.mark.parametrize("mac", ["ff:ff:ff:ff:ff:ff", "01:00:00:00:00:01", "00:00:00:00:00:00"])
async def test_set_never_targets_broadcast_multicast_or_zero(mac):
    transport = SetTransport()
    server, _ = make_server(NetworkBackend("fake0", transport=transport), writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, "set_device_name", device_mac=mac, name="new-name")
    assert not result["success"] and result["meta"]["outcome"] == "not_sent"
    assert transport.sent == []


async def test_signal_rejects_unrepresentable_duration_and_status_reports_scope():
    transport = SetTransport()
    server, _ = make_server(NetworkBackend("fake0", transport=transport), writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, "identify_device", device_mac=MAC, duration_s=5)
    assert not result["success"] and result["meta"]["outcome"] == "not_sent"
    status = await call(server, "ping")
    caps = status["data"]["capabilities"]
    assert caps["operations"]["set_device_name"] == {"supported": True, "enabled": True}
    assert caps["operations"]["read_io_data"]["supported"] is False
    assert caps["device_verified"] is False and transport.sent == []
    assert capabilities("network", writes_enabled=False)["operations"]["set_device_name"]["enabled"] is False


async def test_cancelled_capture_retains_interface_until_worker_exits():
    started, release = threading.Event(), threading.Event()

    class Blocked(SetTransport):
        def exchange(self, frame, timeout, accept):
            self.sent.append(frame)
            started.set()
            release.wait(3)
            return []

    transport = Blocked()
    backend = NetworkBackend("fake0", transport=transport, min_interval=0)
    scope = anyio.CancelScope()

    async def run():
        with scope:
            await backend.set_name(MAC, "new-name")

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        while not started.is_set():
            await anyio.sleep(0.001)
        scope.cancel()
        await anyio.sleep(0.01)
        with pytest.raises(RateLimited, match="still running"):
            await backend.discover(0)
        assert len(transport.sent) == 1
        release.set()
    with anyio.fail_after(2):
        while backend._native_lock.locked():
            await anyio.sleep(0.001)
    assert backend._last_result is None


async def test_cancellation_during_sniffer_start_prevents_send(monkeypatch):
    from types import SimpleNamespace

    request_scope = SimpleNamespace(cancel_called=False)

    class Sniffer(FakeSniffer):
        def start(self):
            super().start()
            request_scope.cancel_called = True

    api = FakeScapy(sniffer_cls=Sniffer)
    backend = NetworkBackend("fake0", transport=dcp.ScapyL2Transport("fake0", scapy=api))
    from profinet_mcp.backends import RequestNotSent

    with tool_call(request_scope), pytest.raises(RequestNotSent):
        await backend.set_name(MAC, "new-name")
    assert api.sent == [] and not backend._native_lock.locked()


async def test_abandoned_queued_lease_cannot_release_a_new_owner_lock():
    from profinet_mcp.adapters.dcp_runtime import NativeAttempt

    lock = threading.Lock()
    lock.acquire()
    attempt = NativeAttempt(lock, float("inf"), lambda: False)
    attempt.abandon()
    assert lock.acquire(blocking=False)  # another call now owns the interface
    calls = []
    with pytest.raises(dcp.DcpError, match="queued"):
        attempt.run(lambda: calls.append(True))
    assert calls == [] and lock.locked()
    lock.release()


async def test_configuration_invalidates_cached_discovery():
    transport = SetTransport()
    backend = NetworkBackend("fake0", transport=transport)
    backend._last_result = (1, 1, [{"device_name": "old"}], {})
    await backend.set_name(MAC, "new-name")
    assert backend._last_result is None
