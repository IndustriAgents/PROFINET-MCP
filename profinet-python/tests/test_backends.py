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
    ProfinetClientError,
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
    assert [d["device_name"] for d in devices] == ["PN-DEVICE-01"]
    assert devices[0]["vendor"] == "MockVendor"  # the mock's own data, not a placeholder
    assert meta["backend"] == "mock" and meta["mock_endpoint"] == f"127.0.0.1:{mock_server.port}"

    payload, meta = await backend.read_io("PN-DEVICE-01", 1, 1, 8)
    assert payload == bytes(8) and meta["direction"] == "input"

    result, _ = await backend.write_io("PN-DEVICE-01", 1, 2, b"\x0a\x0b")
    assert result == {"written_bytes": 2}
    payload, _ = await backend.read_io("PN-DEVICE-01", 1, 2, 2)
    assert payload == b"\x0a\x0b"
    assert bytes(mock_server.devices["PN-DEVICE-01"].areas["1:2"].buffer[:2]) == b"\x0a\x0b"

    result, _ = await backend.identify(MAC, 2)
    assert result["identify_duration_s"] == 2

    result, _ = await backend.set_ip(MAC, "10.0.0.5", "255.255.255.0", "0.0.0.0")
    assert result["ip_address"] == "10.0.0.5"

    result, _ = await backend.set_name(MAC, "renamed-io")
    assert result["previous_name"] == "PN-DEVICE-01"
    devices, _ = await backend.discover(1.0)
    assert devices[0]["device_name"] == "renamed-io" and devices[0]["ip_address"] == "10.0.0.5"


async def test_mock_errors_are_reported_not_hidden(mock_server) -> None:
    backend = backend_for(mock_server)
    with pytest.raises(ProfinetClientError, match="Slot 0/subslot 1 not found"):
        await backend.read_io("PN-DEVICE-01", 0, 1, 4)
    with pytest.raises(ProfinetClientError, match="input area"):
        await backend.write_io("PN-DEVICE-01", 1, 1, b"\x01")
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
        (b"not json\n", "not JSON"),
        (b'{"data": []}\n', "success flag"),
        (b'{"success": true, "data": {"raw_data_hex": "0001"}}\n', "returned 2 bytes, expected 4"),
        (b'{"success": true, "data": {"raw_data_hex": "zz"}}\n', "raw_data_hex"),
        (b"", "Lost the connection"),
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
                await backend.read_io("PN-DEVICE-01", 1, 1, 4)
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
