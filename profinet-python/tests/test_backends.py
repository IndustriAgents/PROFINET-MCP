"""Backends: the mock transport against the real mock, and the network backend's honesty."""

from __future__ import annotations

import json
import re
import time

import anyio
import pytest
from anyio.abc import SocketAttribute
from test_dcp import XID, FakeTransport, identify_response

from profinet_mcp import dcp
from profinet_mcp.backends import (
    ClientDisconnected,
    DeviceRejected,
    MockBackend,
    NetworkBackend,
    OperationNotImplemented,
    OutcomeUnknown,
    ProfinetClientError,
    RateLimited,
    RequestNotSent,
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


#: Replies a fake mock can give after it has (notionally) applied a change.
BAD_REPLIES = {
    "garbage": b"not json\n",
    "no-success-flag": b'{"data": 1}\n',
    "internal-error": b'{"success": false, "error": "Internal mock error: disk full"}\n',
    "wrong-confirmation": b'{"success": true, "data": {"mac_address": "02:00:00:00:00:01", "written_bytes": 999}}\n',
    "trickle-no-newline": None,  # sends bytes forever without ever finishing the line
}


class DroppingMock:
    """A fake mock that applies every request it receives, then never answers properly."""

    def __init__(self, mode: str, answer_after: int | None = None) -> None:
        # "hang-up" closes the connection, "silent" stays open without replying, BAD_REPLIES keys reply badly
        self.mode = mode
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
            elif self.mode == "trickle-no-newline":
                try:
                    for _ in range(600):
                        await stream.send(b"x")
                        await anyio.sleep(0.05)
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    pass  # the client gave up, as it should
            elif self.mode in BAD_REPLIES:
                await stream.send(BAD_REPLIES[self.mode])


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
            with pytest.raises(OutcomeUnknown, match="may have been applied; check before retrying"):
                await operation(backend)
        finally:
            tg.cancel_scope.cancel()
    assert [r["op"] for r in fake.received] == [op]


@pytest.mark.parametrize("mode", list(BAD_REPLIES))
@pytest.mark.parametrize(("op", "operation"), CHANGES, ids=[op for op, _ in CHANGES])
async def test_bad_replies_to_changes_are_unknown_not_rejected(mode: str, op: str, operation) -> None:
    """Rule 1a: after the send, only an explicit refusal is "rejected"; anything else may have been applied."""
    fake = DroppingMock(mode)
    async with anyio.create_task_group() as tg:
        port = await serve(fake, tg)
        backend = MockBackend("127.0.0.1", port, timeout=0.5, max_retries=3, backoff_base=0.01)
        try:
            with anyio.fail_after(10):
                with pytest.raises(OutcomeUnknown, match="may have been applied; check before retrying"):
                    await operation(backend)
        finally:
            tg.cancel_scope.cancel()
    assert [r["op"] for r in fake.received] == [op]


@pytest.mark.parametrize("flag", ['"true"', "1", '"yes"', "null", "false"])
@pytest.mark.parametrize(("op", "operation"), CHANGES, ids=[op for op, _ in CHANGES])
async def test_only_a_literal_true_rejected_flag_counts(op: str, operation, flag: str) -> None:
    """ "rejected": "true" / 1 is not the mock's contract; a change answered with it may have happened."""
    fake = DroppingMock("silent")
    reply = b'{"success": false, "error": "odd", "rejected": ' + flag.encode() + b"}\n"

    async def answer(stream) -> None:
        async with stream:
            fake.received.append(json.loads(await stream.receive()))
            await stream.send(reply)

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, answer)
        try:
            with pytest.raises(OutcomeUnknown, match="may have been applied"):
                await operation(MockBackend("127.0.0.1", port, timeout=1, max_retries=3))
        finally:
            tg.cancel_scope.cancel()
    assert len(fake.received) == 1


@pytest.mark.parametrize(("op", "operation"), CHANGES, ids=[op for op, _ in CHANGES])
async def test_explicit_refusal_is_rejected(op: str, operation) -> None:
    async def refuse(stream) -> None:
        async with stream:
            await stream.receive()
            await stream.send(b'{"success": false, "error": "No device with MAC", "rejected": true}\n')

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, refuse)
        try:
            with pytest.raises(DeviceRejected, match="No device with MAC"):
                await operation(MockBackend("127.0.0.1", port, timeout=1, max_retries=0))
        finally:
            tg.cancel_scope.cancel()


async def test_trickling_peer_cannot_hang_a_read() -> None:
    """Rule 12: a peer that answers garbage forever gives a clean error, within the deadline."""
    fake = DroppingMock("trickle-no-newline")
    async with anyio.create_task_group() as tg:
        port = await serve(fake, tg)
        backend = MockBackend("127.0.0.1", port, timeout=0.5, max_retries=1, backoff_base=0.01)
        started = time.monotonic()
        try:
            with anyio.fail_after(10):
                with pytest.raises(ProfinetClientError, match="did not answer 'read'") as raised:
                    await backend.read_io("x", 1, 1, 2)
        finally:
            tg.cancel_scope.cancel()
    assert not isinstance(raised.value, OutcomeUnknown)
    assert time.monotonic() - started < backend.deadline()


async def test_request_that_cannot_be_encoded_is_not_sent() -> None:
    backend = MockBackend("127.0.0.1", 1, timeout=1, max_retries=0)
    with pytest.raises(RequestNotSent, match="Could not encode"):
        await backend.call({"op": "write", "data": [float("nan")]}, changes_device=True)


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


async def test_device_lookups_reuse_a_fresh_discovery(monkeypatch) -> None:
    """Minor 5: discover then get_device_info must work within the 2 s interval, from the cached answer."""
    now = [50.0]
    transport = FakeTransport([identify_response(xid=XID)])
    monkeypatch.setattr(dcp.random, "getrandbits", lambda bits: XID)
    backend = NetworkBackend("fake0", transport=transport, clock=lambda: now[0])
    devices, meta = await backend.discover(0.5)
    assert meta["cached"] is False and len(transport.sent) == 1
    now[0] += 0.7
    cached, meta = await backend.discover(0.5, allow_cached=True)
    assert cached == devices
    assert meta["cached"] is True and meta["age_s"] == pytest.approx(0.7)
    assert len(transport.sent) == 1  # nothing new went out
    with pytest.raises(RateLimited):  # an explicit fresh discovery is still refused
        await backend.discover(0.5)
    now[0] += 1.5
    _, meta = await backend.discover(0.5, allow_cached=True)  # older than the interval: fresh request
    assert meta["cached"] is False and len(transport.sent) == 2


# -- the MCP client disconnects while a request is pending (rules 1 and 12) -------------------


async def closed_port() -> int:
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    await listener.aclose()
    return port


async def count_arrivals(port: int, seconds: float) -> list[bytes]:
    """Listen on ``port`` for a while and return whatever arrives."""
    arrivals: list[bytes] = []

    async def record(stream) -> None:
        async with stream:
            try:
                with anyio.fail_after(1):
                    arrivals.append(await stream.receive())
            except (TimeoutError, anyio.EndOfStream):
                arrivals.append(b"<connected>")

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=port)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, record)
        await anyio.sleep(seconds)
        tg.cancel_scope.cancel()
    return arrivals


@pytest.mark.parametrize(("op", "operation"), CHANGES, ids=[op for op, _ in CHANGES])
async def test_disconnect_during_retry_backoff_sends_nothing(op: str, operation) -> None:
    port = await closed_port()
    backend = MockBackend("127.0.0.1", port, timeout=5, max_retries=5, backoff_base=1.0)
    outcome: dict = {}

    async def run() -> None:
        try:
            await operation(backend)
        except Exception as exc:  # noqa: BLE001 - recorded for the assertions
            outcome["error"] = exc

    async with anyio.create_task_group() as tg:
        tg.start_soon(run)
        await anyio.sleep(0.3)  # first connect refused; now waiting 1 s to retry
        backend.shutdown()
    assert isinstance(outcome["error"], ClientDisconnected), outcome
    assert await count_arrivals(port, 2.5) == []  # the retry never happens


async def test_disconnect_during_a_hanging_connect_sends_nothing(monkeypatch) -> None:
    attempts = []

    async def hanging_connect(host, port, **kwargs):
        attempts.append(port)
        await anyio.sleep(60)

    monkeypatch.setattr(anyio, "connect_tcp", hanging_connect)
    backend = MockBackend("127.0.0.1", 9, timeout=30, max_retries=0)
    outcome: dict = {}

    async def run() -> None:
        try:
            await backend.write_io("x", 1, 2, b"\x01")
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(run)
            await anyio.sleep(0.2)
            backend.shutdown()
    assert attempts and isinstance(outcome["error"], ClientDisconnected)


async def test_disconnect_between_connect_and_send_sends_nothing(monkeypatch) -> None:
    received: list[bytes] = []

    async def record(stream) -> None:
        async with stream:
            try:
                received.append(await stream.receive())
            except anyio.EndOfStream:
                pass

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    backend = MockBackend("127.0.0.1", port, timeout=5, max_retries=0)
    real_connect = anyio.connect_tcp

    async def connect_then_disconnect(*args, **kwargs):
        stream = await real_connect(*args, **kwargs)
        backend.shutdown()  # the client leaves right after the connection is up
        return stream

    monkeypatch.setattr(anyio, "connect_tcp", connect_then_disconnect)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, record)
        try:
            with pytest.raises(ClientDisconnected):
                await backend.set_name(MAC, "late-name")
            await anyio.sleep(0.3)
        finally:
            tg.cancel_scope.cancel()
    assert received == []


async def test_disconnect_while_waiting_for_the_reply_is_unknown_for_changes() -> None:
    fake = DroppingMock("silent")
    async with anyio.create_task_group() as tg:
        port = await serve(fake, tg)
        backend = MockBackend("127.0.0.1", port, timeout=10, max_retries=3)
        outcomes: dict = {}

        async def run(name, operation) -> None:
            try:
                await operation()
            except Exception as exc:  # noqa: BLE001
                outcomes[name] = exc

        async with anyio.create_task_group() as calls:
            calls.start_soon(run, "write", lambda: backend.write_io("x", 1, 2, b"\x01"))
            calls.start_soon(run, "read", lambda: backend.read_io("x", 1, 1, 2))
            await anyio.sleep(0.5)
            backend.shutdown()
        tg.cancel_scope.cancel()
    assert isinstance(outcomes["write"], OutcomeUnknown) and "may have been applied" in str(outcomes["write"])
    assert type(outcomes["read"]) is ProfinetClientError and "disconnected" in str(outcomes["read"])
    assert sorted(r["op"] for r in fake.received) == ["read", "write"]  # each sent once, never again


async def test_no_request_starts_after_shutdown() -> None:
    port = await closed_port()
    backend = MockBackend("127.0.0.1", port, timeout=5, max_retries=3)
    backend.shutdown()
    with pytest.raises(ClientDisconnected):
        await backend.write_io("x", 1, 2, b"\x01")
    with pytest.raises(ClientDisconnected):
        await backend.discover(1.0)
    network = NetworkBackend("fake0", transport=FakeTransport([]))
    network.shutdown()
    with pytest.raises(ClientDisconnected):
        await network.discover(0.5)
    assert network.transport.sent == []


def test_default_deadlines_stay_under_fifty_seconds() -> None:
    """Review minor: with default settings no call can take longer than ~50 s, client backstop included."""
    config = ProfinetConfig(backend="mock")
    mock = create_backend(config)
    network = create_backend(ProfinetConfig(backend="network"))
    worst = [
        mock.deadline(None),  # read, write, identify, set_name, set_ip
        mock.deadline(config.dcp_timeout),  # discover via the mock
        network.deadline(config.dcp_timeout),  # DCP discovery
    ]
    assert all(deadline + 2.0 < 50 for deadline in worst), worst


async def test_deadline_expiring_before_the_send_is_not_sent(monkeypatch) -> None:
    async def hanging_connect(host, port, **kwargs):
        await anyio.sleep(60)

    monkeypatch.setattr(anyio, "connect_tcp", hanging_connect)
    backend = MockBackend("127.0.0.1", 9, timeout=30, max_retries=0)
    monkeypatch.setattr(backend, "deadline", lambda timeout=None: 0.3)
    with anyio.fail_after(5):
        with pytest.raises(RequestNotSent, match="was not sent within its 0.3 s deadline"):
            await backend.write_io("x", 1, 2, b"\x01")


async def test_deadline_expiring_after_the_send_is_unknown(monkeypatch) -> None:
    fake = DroppingMock("silent")
    async with anyio.create_task_group() as tg:
        port = await serve(fake, tg)
        backend = MockBackend("127.0.0.1", port, timeout=30, max_retries=0)
        monkeypatch.setattr(backend, "deadline", lambda timeout=None: 0.5)
        try:
            with anyio.fail_after(5):
                with pytest.raises(OutcomeUnknown, match="deadline expired"):
                    await backend.set_ip(MAC, "10.0.0.2", "255.0.0.0", "0.0.0.0")
        finally:
            tg.cancel_scope.cancel()
    assert [r["op"] for r in fake.received] == ["set_ip"]
