"""Backends: the mock transport against the real mock, and the network backend's honesty."""

from __future__ import annotations

import json
import re

import anyio
import pytest
from anyio.abc import SocketAttribute
from test_dcp import XID, FakeTransport, identify_response

from profinet_mcp import dcp
from profinet_mcp.backends import (
    MockBackend,
    NetworkBackend,
    OperationNotImplemented,
    OutcomeUnknown,
    ProfinetClientError,
    RateLimited,
    UnconfiguredBackend,
    create_backend,
)
from profinet_mcp.config import ProfinetConfig

pytestmark = pytest.mark.anyio

MAC = "00:11:22:33:44:55"


def backend_for(server, **kwargs) -> MockBackend:
    return MockBackend("127.0.0.1", server.port, timeout=5, max_retries=0, **kwargs)


async def test_mock_backend_drives_every_operation(mock_server) -> None:
    backend = backend_for(mock_server)

    devices, meta = await backend.discover(1.0)
    assert [d["device_name"] for d in devices] == ["pn-device-01"]
    assert devices[0]["vendor"] == "MockVendor"  # the mock's own data, not a placeholder
    assert meta["backend"] == "mock" and meta["mock_endpoint"] == f"127.0.0.1:{mock_server.port}"

    payload, meta = await backend.read_io("pn-device-01", 1, 1, 8)
    assert payload == bytes(8) and meta["direction"] == "input"

    result, _ = await backend.write_io("pn-device-01", 1, 2, b"\x0a\x0b")
    assert result == {"written_bytes": 2}
    payload, _ = await backend.read_io("pn-device-01", 1, 2, 2)
    assert payload == b"\x0a\x0b"
    assert bytes(mock_server.devices["pn-device-01"].areas["1:2"].buffer[:2]) == b"\x0a\x0b"

    result, _ = await backend.identify(MAC, 2)
    assert result["identify_duration_s"] == 2

    result, _ = await backend.set_ip(MAC, "10.0.0.5", "255.255.255.0", "0.0.0.0")
    assert result["ip_address"] == "10.0.0.5"

    result, _ = await backend.set_name(MAC, "renamed-io")
    assert result["previous_name"] == "pn-device-01"
    devices, _ = await backend.discover(1.0)
    assert devices[0]["device_name"] == "renamed-io" and devices[0]["ip_address"] == "10.0.0.5"


async def test_mock_errors_are_reported_not_hidden(mock_server) -> None:
    backend = backend_for(mock_server)
    with pytest.raises(ProfinetClientError, match="Slot 0/subslot 1 not found"):
        await backend.read_io("pn-device-01", 0, 1, 4)
    with pytest.raises(ProfinetClientError, match="input area"):
        await backend.write_io("pn-device-01", 1, 1, b"\x01")
    with pytest.raises(ProfinetClientError, match="No device with MAC"):
        await backend.identify("02:00:00:00:00:99", 1)


async def test_unreachable_mock_is_an_error_after_retries() -> None:
    # Bind and close a listener to get a port nobody listens on.
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    await listener.aclose()
    backend = MockBackend("127.0.0.1", port, timeout=1, max_retries=1, backoff_base=0.01)
    with pytest.raises(ProfinetClientError, match=r"Cannot reach the mock IO device .*2 attempt"):
        await backend.discover(1.0)
    probe = await backend.probe()
    assert probe["reachable"] is False


@pytest.mark.parametrize(
    ("answer", "fragment"),
    [
        (b"not json\n", "not JSON with a success flag"),
        (b'{"data": []}\n', "success flag"),
        (b'{"success": true, "data": {"raw_data_hex": "0001"}}\n', "returned 2 bytes, expected 4"),
        (b'{"success": true, "data": {"raw_data_hex": "zz"}}\n', "raw_data_hex"),
        (b"", "connection was lost"),
    ],
)
async def test_malformed_mock_answers_are_rejected(answer: bytes, fragment: str) -> None:
    async def handle(stream) -> None:
        async with stream:
            await stream.receive()
            if answer:
                await stream.send(answer)

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, handle)
        try:
            backend = MockBackend("127.0.0.1", port, timeout=2, max_retries=0)
            with pytest.raises(ProfinetClientError, match=fragment):
                await backend.read_io("pn-device-01", 1, 1, 4)
        finally:
            tg.cancel_scope.cancel()


async def test_unconfirmed_write_is_not_reported_as_success() -> None:
    async def handle(stream) -> None:
        async with stream:
            await stream.receive()
            await stream.send(json.dumps({"success": True, "data": {"written_bytes": 1}}).encode() + b"\n")

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, handle)
        try:
            with pytest.raises(ProfinetClientError, match="confirmed 1 written bytes, expected 3"):
                await MockBackend("127.0.0.1", port, timeout=2, max_retries=0).write_io("x", 1, 2, b"abc")
        finally:
            tg.cancel_scope.cancel()


class DroppingMock:
    """A fake mock that applies every request it receives, then never answers (or hangs up)."""

    def __init__(self, mode: str, answer_after: int | None = None) -> None:
        self.mode = mode  # "hang-up" closes the connection, "silent" keeps it open without replying
        self.received: list[dict] = []
        self.answer_after = answer_after  # answer normally once this many requests were dropped

    async def handle(self, stream) -> None:
        async with stream:
            request = json.loads(await stream.receive())
            self.received.append(request)
            if self.answer_after is not None and len(self.received) > self.answer_after:
                data = {"device_name": "x", "raw_data_hex": "0102"}
                await stream.send(json.dumps({"success": True, "data": data}).encode() + b"\n")
                return
            if self.mode == "silent":
                await anyio.sleep(30)


async def serve(fake: DroppingMock, task_group) -> int:
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    task_group.start_soon(listener.serve, fake.handle)
    return listener.extra(SocketAttribute.local_port)


CHANGES = [
    ("write", lambda b: b.write_io("pn-device-01", 1, 2, b"\x01\x02")),
    ("set_name", lambda b: b.set_name(MAC, "new-name")),
    ("set_ip", lambda b: b.set_ip(MAC, "10.0.0.2", "255.0.0.0", "0.0.0.0")),
    ("identify", lambda b: b.identify(MAC, 5)),
]


@pytest.mark.parametrize("mode", ["hang-up", "silent"])
@pytest.mark.parametrize(("op", "operation"), CHANGES, ids=[op for op, _ in CHANGES])
async def test_changes_are_never_sent_twice(mode: str, op: str, operation) -> None:
    """A write or config command whose reply is lost must reach the device exactly once."""
    fake = DroppingMock(mode)
    async with anyio.create_task_group() as tg:
        port = await serve(fake, tg)
        backend = MockBackend("127.0.0.1", port, timeout=0.5, max_retries=3, backoff_base=0.01)
        try:
            with pytest.raises(OutcomeUnknown, match="may have been applied. It was not sent again"):
                await operation(backend)
        finally:
            tg.cancel_scope.cancel()
    assert [r["op"] for r in fake.received] == [op]


async def test_reads_are_retried_when_the_reply_is_lost() -> None:
    fake = DroppingMock("hang-up", answer_after=2)
    async with anyio.create_task_group() as tg:
        port = await serve(fake, tg)
        backend = MockBackend("127.0.0.1", port, timeout=0.5, max_retries=3, backoff_base=0.01)
        try:
            payload, _ = await backend.read_io("x", 1, 1, 2)
        finally:
            tg.cancel_scope.cancel()
    assert payload == b"\x01\x02"
    assert [r["op"] for r in fake.received] == ["read", "read", "read"]


async def test_reads_give_up_after_max_retries() -> None:
    fake = DroppingMock("hang-up")
    async with anyio.create_task_group() as tg:
        port = await serve(fake, tg)
        backend = MockBackend("127.0.0.1", port, timeout=0.5, max_retries=1, backoff_base=0.01)
        try:
            with pytest.raises(ProfinetClientError, match="did not answer 'read'") as raised:
                await backend.read_io("x", 1, 1, 2)
        finally:
            tg.cancel_scope.cancel()
    assert not isinstance(raised.value, OutcomeUnknown)
    assert len(fake.received) == 2


async def test_unreachable_mock_write_is_not_an_unknown_outcome() -> None:
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    await listener.aclose()
    backend = MockBackend("127.0.0.1", port, timeout=1, max_retries=2, backoff_base=0.01)
    with pytest.raises(ProfinetClientError, match="Cannot reach the mock IO device .*3 attempt") as raised:
        await backend.write_io("x", 1, 2, b"\x01")  # connecting is retried: nothing had been sent
    assert not isinstance(raised.value, OutcomeUnknown)


async def test_network_discovery_is_rate_limited(monkeypatch) -> None:
    now = [100.0]
    transport = FakeTransport([])
    backend = NetworkBackend("fake0", transport=transport, clock=lambda: now[0])
    await backend.discover(0.5)
    now[0] += 0.5
    with pytest.raises(RateLimited, match="Nothing was sent; try again in 1.5 s") as raised:
        await backend.discover(0.5)
    assert raised.value.retry_after == pytest.approx(1.5)
    assert len(transport.sent) == 1
    now[0] += 1.5
    await backend.discover(0.5)
    assert len(transport.sent) == 2


async def test_failed_discovery_does_not_count_against_the_rate_limit() -> None:
    class Broken(FakeTransport):
        def local_mac(self) -> str:
            raise dcp.DcpError("Interface 'nonexistent0' was not found")

    backend = NetworkBackend("nonexistent0", transport=Broken([]), clock=lambda: 5.0)
    for _ in range(2):  # the second call is not refused as "too soon"
        with pytest.raises(ProfinetClientError, match="nonexistent0"):
            await backend.discover(0.5)


async def test_network_backend_discovers_with_dcp(monkeypatch) -> None:
    backend = NetworkBackend("fake0", transport=FakeTransport([identify_response(xid=XID)]))
    monkeypatch.setattr(dcp.random, "getrandbits", lambda bits: XID)
    devices, meta = await backend.discover(0.05)
    assert [d["device_name"] for d in devices] == ["line1-io"]
    assert meta["backend"] == "network" and meta["protocol"] == "DCP Identify All"
    assert meta["interface"] == "fake0"


@pytest.mark.parametrize(
    ("operation", "fragment"),
    [
        (lambda b: b.read_io("x", 1, 1, 4), "IO-controller stack"),
        (lambda b: b.write_io("x", 1, 1, b"\x01"), "IO-controller stack"),
        (lambda b: b.identify(MAC, 5), "DCP Set (identification signal)"),
        (lambda b: b.set_name(MAC, "new-name"), "DCP Set (name of station)"),
        (lambda b: b.set_ip(MAC, "10.0.0.2", "255.0.0.0", "0.0.0.0"), "DCP Set (IP parameters)"),
    ],
)
async def test_network_backend_refuses_what_it_cannot_do(operation, fragment: str) -> None:
    transport = FakeTransport([])
    with pytest.raises(OperationNotImplemented, match=re.escape(fragment)):
        await operation(NetworkBackend("fake0", transport=transport))
    assert transport.sent == []  # nothing went on the wire


async def test_create_backend_follows_config() -> None:
    assert isinstance(create_backend(ProfinetConfig()), UnconfiguredBackend)
    assert isinstance(create_backend(ProfinetConfig(backend="mock")), MockBackend)
    assert isinstance(create_backend(ProfinetConfig(backend="network")), NetworkBackend)
