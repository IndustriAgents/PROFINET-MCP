"""Tool behaviour through FastMCP: descriptions, envelopes, validation, safety switches, honesty."""

from __future__ import annotations

import json
from typing import Any

import anyio
import pytest
from anyio.abc import SocketAttribute
from helpers import SAMPLE_GSDML, envelope_of
from test_dcp import FakeTransport

from profinet_mcp.backends import MockBackend, NetworkBackend
from profinet_mcp.config import ProfinetConfig
from profinet_mcp.pn_client import ProfinetClient
from profinet_mcp.server import ProfinetMCPServer

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "discover_devices",
    "get_device_info",
    "set_device_name",
    "set_device_ip",
    "identify_device",
    "read_io_data",
    "write_io_data",
    "load_gsd_file",
    "list_devices",
    "read_device_by_alias",
    "write_device_by_alias",
    "ping",
    "get_connection_status",
    "test_device_communication",
}
MAC = "00:11:22:33:44:55"
DEVICE = {"device_name": "line1-io", "mac_address": MAC, "ip_address": "192.168.1.100"}


class RecordingBackend:
    """A backend that records calls; used to prove what was (not) sent."""

    name = "mock"

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.fail = fail

    async def _call(self, op: str, *args: Any) -> None:
        self.calls.append((op, args))
        if self.fail is not None:
            raise self.fail

    async def discover(self, timeout):
        await self._call("discover", timeout)
        return [dict(DEVICE)], {"backend": self.name}

    async def read_io(self, device, slot, subslot, length):
        await self._call("read_io", device, slot, subslot, length)
        return bytes(range(length)), {"backend": self.name}

    async def write_io(self, device, slot, subslot, data):
        await self._call("write_io", device, slot, subslot, data)
        return {"written_bytes": len(data)}, {"backend": self.name}

    async def identify(self, mac, duration_s):
        await self._call("identify", mac, duration_s)
        return {"mac_address": mac, "identify_duration_s": duration_s}, {"backend": self.name}

    async def set_name(self, mac, name):
        await self._call("set_name", mac, name)
        return {"mac_address": mac, "device_name": name}, {"backend": self.name}

    async def set_ip(self, mac, ip, subnet, gateway):
        await self._call("set_ip", mac, ip, subnet, gateway)
        return {"mac_address": mac, "ip_address": ip, "subnet_mask": subnet, "gateway": gateway}, {"backend": self.name}

    def describe(self):
        return {"backend": self.name}

    async def probe(self):
        return {"reachable": True, "detail": "fake"}


def make_server(backend=None, **settings) -> tuple[ProfinetMCPServer, Any]:
    backend = backend if backend is not None else RecordingBackend()
    config = ProfinetConfig(backend=getattr(backend, "name", "none"), **settings)
    return ProfinetMCPServer(client=ProfinetClient(config, backend)), backend


async def call(server: ProfinetMCPServer, tool: str, **arguments: Any) -> dict[str, Any]:
    result = envelope_of(await server.mcp.call_tool(tool, arguments))
    assert set(result) == {"success", "data", "error", "meta"}, result
    assert result["meta"]["tool"] == tool
    return result


# -- discovery of the tools themselves -------------------------------------------------


async def test_every_tool_has_a_description_and_schema() -> None:
    server, _ = make_server()
    tools = await server.mcp.list_tools()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description and len(tool.description) > 40, tool.name
        assert tool.annotations is not None, tool.name
        for name, schema in tool.inputSchema.get("properties", {}).items():
            assert schema.get("description"), f"{tool.name}.{name} has no description"
    by_name = {tool.name: tool for tool in tools}
    assert by_name["write_io_data"].annotations.destructiveHint is True
    assert by_name["read_io_data"].annotations.readOnlyHint is True


# -- argument validation comes back as an envelope ----------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "fragment"),
    [
        ("read_io_data", {"device_name": "x", "slot": "one", "subslot": 1, "data_length": 4}, "slot"),
        ("read_io_data", {"device_name": "x", "slot": 1, "subslot": 0, "data_length": 4}, "subslot"),
        ("read_io_data", {"device_name": "x", "slot": 1, "subslot": 1, "data_length": 0}, "data_length"),
        ("read_io_data", {"device_name": "x", "slot": 1, "subslot": 1, "data_length": 1441}, "data_length"),
        ("read_io_data", {"slot": 1, "subslot": 1, "data_length": 4}, "device_name: Field required"),
        ("read_io_data", {"device_name": "   ", "slot": 1, "subslot": 1, "data_length": 4}, "non-empty"),
        ("discover_devices", {"timeout": -1}, "timeout"),
        ("discover_devices", {"timeout": "soon"}, "timeout"),
        ("discover_devices", {"timeout": 0.0001}, "timeout: Input should be greater than or equal to 0.5"),
        ("discover_devices", {"timeout": True}, "timeout"),
        ("discover_devices", {"timeout": "5"}, "timeout"),
        ("read_io_data", {"device_name": "x", "slot": True, "subslot": 1, "data_length": 4}, "slot"),
        ("read_io_data", {"device_name": "x", "slot": 1, "subslot": "1", "data_length": 4}, "subslot"),
        ("read_io_data", {"device_name": "x", "slot": 1, "subslot": 1, "data_length": 8.0}, "data_length"),
        ("identify_device", {"device_mac": MAC, "duration_s": "5"}, "duration_s"),
        ("identify_device", {"device_mac": MAC, "duration_s": 5.0}, "duration_s"),
        ("identify_device", {"device_mac": "zz:11:22:33:44:55"}, "not a MAC address"),
        ("identify_device", {"device_mac": "01:00:5e:00:00:01"}, "multicast"),
        ("identify_device", {"device_mac": MAC, "duration_s": 0}, "duration_s"),
        ("get_device_info", {"device_name": ""}, "device_name"),
        ("load_gsd_file", {"filepath": ""}, "filepath"),
    ],
)
async def test_bad_arguments_are_envelopes(tool: str, arguments: dict, fragment: str) -> None:
    server, backend = make_server(config_cmds_enabled=True)
    result = await call(server, tool, **arguments)
    assert result["success"] is False
    assert result["error"].startswith("Invalid arguments:")
    assert fragment in result["error"]
    assert backend.calls == []


@pytest.mark.parametrize("data", [[256], [-1], ["a"], [], [1.5], [True], ["7"], [2.0]])
async def test_write_values_are_range_checked_not_masked(data) -> None:
    server, backend = make_server(writes_enabled=True)
    result = await call(server, "write_io_data", device_name="x", slot=1, subslot=1, data=data)
    assert result["success"] is False and "Invalid arguments" in result["error"]
    assert backend.calls == []  # before the fix 256 was silently written as 0, and True/"7"/2.0 coerced


async def test_lax_coercion_is_rejected() -> None:
    """Regression: slot=True, subslot="1", data=[True, "7", 2.0] used to write b"\\x01\\x07\\x02" to slot 1."""
    server, backend = make_server(writes_enabled=True)
    result = await call(server, "write_io_data", device_name="x", slot=True, subslot="1", data=[True, "7", 2.0])
    assert result["success"] is False
    assert {p["argument"] for p in result["meta"]["invalid_arguments"]} == {
        "slot",
        "subslot",
        "data.0",
        "data.1",
        "data.2",
    }
    assert backend.calls == []


# -- safety switches ----------------------------------------------------------------------


async def test_writes_are_refused_by_default() -> None:
    server, backend = make_server()
    assert server.config.writes_enabled is False
    for tool, args in [
        ("write_io_data", {"device_name": "x", "slot": 1, "subslot": 1, "data": [1]}),
        ("write_device_by_alias", {"alias": "anything", "data": [1]}),
    ]:
        result = await call(server, tool, **args)
        assert result["success"] is False
        assert "PROFINET_WRITES_ENABLED=true" in result["error"]
        assert result["meta"]["refused"] is True
    assert backend.calls == []


async def test_config_commands_are_refused_by_default() -> None:
    server, backend = make_server(writes_enabled=True)
    for tool, args in [
        ("set_device_name", {"device_mac": MAC, "name": "new-name"}),
        ("set_device_ip", {"device_mac": MAC, "ip_address": "10.0.0.2", "subnet_mask": "255.255.255.0"}),
        ("identify_device", {"device_mac": MAC, "duration_s": 600}),  # DCP Set Signal is a config command
    ]:
        result = await call(server, tool, **args)
        assert result["success"] is False
        assert "PROFINET_CONFIG_CMDS_ENABLED=true" in result["error"]
        assert result["meta"]["refused"] is True
    assert backend.calls == []


async def test_client_gates_identify_too() -> None:
    """The client refuses on its own, so a caller that bypasses the tools cannot flash the LED either."""
    from profinet_mcp.pn_client import OperationRefused

    backend = RecordingBackend()
    client = ProfinetClient(ProfinetConfig(backend="mock"), backend)
    with pytest.raises(OperationRefused, match="identify_device"):
        await client.identify_device(MAC, 5)
    assert backend.calls == []


async def test_enabled_writes_and_config_commands_reach_the_backend() -> None:
    server, backend = make_server(writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, "write_io_data", device_name="line1-io", slot=1, subslot=2, data=[1, 255])
    assert result["success"] is True and result["data"]["written_bytes"] == 2
    assert result["meta"]["backend"] == "mock" and "duration_ms" in result["meta"]

    result = await call(server, "set_device_name", device_mac=MAC.upper(), name="press-2.hall-b")
    assert result["success"] is True
    result = await call(
        server, "set_device_ip", device_mac=MAC, ip_address="10.0.0.2", subnet_mask="255.255.255.0", gateway="10.0.0.1"
    )
    assert result["success"] is True
    assert backend.calls == [
        ("write_io", ("line1-io", 1, 2, b"\x01\xff")),
        ("set_name", (MAC, "press-2.hall-b")),
        ("set_ip", (MAC, "10.0.0.2", "255.255.255.0", "10.0.0.1")),
    ]


@pytest.mark.parametrize(
    ("name", "fragment"),
    [("Line1", "lowercase"), ("a_b", "may only contain"), ("port-123", "port-xyz"), ("10.0.0.1", "IP address")],
)
async def test_station_names_are_validated(name: str, fragment: str) -> None:
    server, backend = make_server(config_cmds_enabled=True)
    result = await call(server, "set_device_name", device_mac=MAC, name=name)
    assert result["success"] is False and fragment in result["error"]
    assert backend.calls == []


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({"ip_address": "10.0.0.300", "subnet_mask": "255.255.255.0"}, "not an IPv4"),
        ({"ip_address": "10.0.0.2", "subnet_mask": "255.0.255.0"}, "contiguous"),
        ({"ip_address": "10.0.0.255", "subnet_mask": "255.255.255.0"}, "broadcast"),
        ({"ip_address": "10.0.0.2", "subnet_mask": "255.255.255.0", "gateway": "10.9.9.9"}, "not inside"),
    ],
)
async def test_ip_settings_are_validated(args: dict, fragment: str) -> None:
    server, backend = make_server(config_cmds_enabled=True)
    result = await call(server, "set_device_ip", device_mac=MAC, **args)
    assert result["success"] is False and fragment in result["error"]
    assert backend.calls == []


# -- reads and discovery --------------------------------------------------------------------


async def test_read_and_discovery_tools() -> None:
    server, backend = make_server(config_cmds_enabled=True)
    result = await call(server, "read_io_data", device_name="line1-io", slot=1, subslot=1, data_length=3)
    assert result["success"] is True
    assert result["data"]["raw_data_hex"] == "000102" and result["data"]["bytes"] == [0, 1, 2]
    assert result["meta"]["backend"] == "mock"

    result = await call(server, "discover_devices", timeout=0.5)
    assert result["data"]["count"] == 1 and backend.calls[-1] == ("discover", (0.5,))

    for ref in ("LINE1-IO", "192.168.1.100", "00-11-22-33-44-55"):
        result = await call(server, "get_device_info", device_name=ref)
        assert result["success"] is True and result["data"]["device_name"] == "line1-io", ref
    result = await call(server, "get_device_info", device_name="ghost")
    assert result["success"] is False and "ghost" in result["error"]

    result = await call(server, "test_device_communication", device_name="line1-io")
    assert result["success"] is True and result["data"]["reachable"] is True
    result = await call(server, "test_device_communication", device_name="ghost")
    assert result["success"] is False and result["meta"]["reachable"] is False

    result = await call(server, "identify_device", device_mac=MAC, duration_s=3)
    assert result["success"] is True and backend.calls[-1] == ("identify", (MAC, 3))


async def test_backend_failures_are_envelopes() -> None:
    server, _ = make_server(RecordingBackend(fail=RuntimeError("boom")))
    result = await call(server, "discover_devices")
    assert result["success"] is False and result["error"] == "Internal error: RuntimeError: boom"


# -- no placeholder data anywhere -----------------------------------------------------------


async def test_without_a_backend_nothing_is_invented() -> None:
    server = ProfinetMCPServer(ProfinetConfig(config_cmds_enabled=True))
    for tool, args in [
        ("discover_devices", {}),
        ("get_device_info", {"device_name": "PN-DEVICE-01"}),
        ("read_io_data", {"device_name": "PN-DEVICE-01", "slot": 1, "subslot": 1, "data_length": 8}),
        ("test_device_communication", {"device_name": "PN-DEVICE-01"}),
        ("identify_device", {"device_mac": MAC}),
    ]:
        result = await call(server, tool, **args)
        assert result["success"] is False, tool
        assert result["data"] is None
        assert "PROFINET_BACKEND" in result["error"]
        assert result["meta"]["backend"] == "none"


async def test_network_backend_is_honest() -> None:
    transport = FakeTransport([])
    backend = NetworkBackend("fake0", transport=transport)
    server, _ = make_server(backend, writes_enabled=True, config_cmds_enabled=True)
    for tool, args in [
        ("read_io_data", {"device_name": "line1-io", "slot": 1, "subslot": 1, "data_length": 8}),
        ("write_io_data", {"device_name": "line1-io", "slot": 1, "subslot": 1, "data": [1]}),
        ("identify_device", {"device_mac": MAC}),
        ("set_device_name", {"device_mac": MAC, "name": "abc"}),
        ("set_device_ip", {"device_mac": MAC, "ip_address": "10.0.0.2", "subnet_mask": "255.0.0.0"}),
    ]:
        result = await call(server, tool, **args)
        assert result["success"] is False, tool
        assert result["error"].startswith("Not implemented on the network backend"), tool
        assert result["meta"] == {"tool": tool, "backend": "network", "not_implemented": True}
    assert transport.sent == []

    result = await call(server, "discover_devices", timeout=0.5)
    assert result["success"] is True and result["data"] == {"devices": [], "count": 0}
    assert result["meta"]["backend"] == "network" and result["meta"]["protocol"] == "DCP Identify All"
    assert len(transport.sent) == 1

    again = await call(server, "discover_devices", timeout=0.5)  # immediately: refused, nothing sent
    assert again["success"] is False and "Nothing was sent" in again["error"]
    assert again["meta"]["refused"] is True and again["meta"]["retry_after_s"] > 0
    assert len(transport.sent) == 1


# -- device map aliases ---------------------------------------------------------------------


async def test_alias_tools(tmp_path) -> None:
    device_map = tmp_path / "devices.json"
    device_map.write_text(
        json.dumps(
            {
                "line1_inputs": {"device_name": "line1-io", "slot": 1, "subslot": 1, "data_length": 4},
                "line1_outputs": {"device_name": "line1-io", "slot": 1, "subslot": 2, "data_length": 2},
                "broken_slot": {"device_name": "line1-io", "slot": "one"},
                "not_an_object": 5,
            }
        )
    )
    server, backend = make_server(writes_enabled=True, device_map_path=device_map)

    listed = await call(server, "list_devices")
    assert listed["success"] is True
    assert [d["alias"] for d in listed["data"]["devices"]] == ["line1_inputs", "line1_outputs", "broken_slot"]
    assert "not_an_object" in listed["data"]["warning"]

    result = await call(server, "read_device_by_alias", alias="line1_inputs")
    assert result["success"] is True and result["data"]["alias"] == "line1_inputs"
    assert result["data"]["raw_data_hex"] == "00010203"

    result = await call(server, "write_device_by_alias", alias="line1_outputs", data=[7, 8])
    assert result["success"] is True and backend.calls[-1] == ("write_io", ("line1-io", 1, 2, b"\x07\x08"))

    result = await call(server, "read_device_by_alias", alias="broken_slot")
    assert result["success"] is False and "slot must be an integer" in result["error"]
    result = await call(server, "read_device_by_alias", alias="nope")
    assert result["success"] is False and "line1_inputs" in result["error"]

    device_map.write_text("{ not json")
    listed = await call(server, "list_devices")
    assert listed["success"] is False and "not valid JSON" in listed["error"]
    ping = await call(server, "ping")
    assert "not valid JSON" in ping["data"]["device_map_error"]


# -- GSDML, ping, connection status -----------------------------------------------------------


async def test_load_gsd_file_tool(tmp_path) -> None:
    server, _ = make_server(gsd_base_path=SAMPLE_GSDML.parent)
    result = await call(server, "load_gsd_file", filepath=SAMPLE_GSDML.name)
    assert result["success"] is True
    assert result["data"]["module_count"] == 4
    assert result["data"]["metadata"]["vendor_id"] == "0x0F00"
    assert result["meta"] == {
        "tool": "load_gsd_file",
        "source": "gsdml_file",
        "filepath": SAMPLE_GSDML.name,
        "cached": False,
    }
    again = await call(server, "load_gsd_file", filepath=str(SAMPLE_GSDML))
    assert again["meta"]["cached"] is True

    bad = tmp_path / "bad.xml"
    bad.write_text("<nope/>")
    result = await call(server, "load_gsd_file", filepath=str(bad))
    assert result["success"] is False and "not a GSDML file" in result["error"]
    result = await call(server, "load_gsd_file", filepath=str(tmp_path / "missing.xml"))
    assert result["success"] is False and "not found" in result["error"]


async def test_ping_and_connection_status(mock_server) -> None:
    config = ProfinetConfig(backend="mock", mock_port=mock_server.port, max_retries=0)
    server = ProfinetMCPServer(config)
    ping = await call(server, "ping")
    assert ping["success"] is True
    assert ping["data"]["backend"] == "mock"
    assert ping["data"]["writes_enabled"] is False and ping["data"]["config_cmds_enabled"] is False
    assert ping["data"]["connection"]["mock_endpoint"] == f"127.0.0.1:{mock_server.port}"
    assert ping["meta"]["contacted"] is False

    status = await call(server, "get_connection_status")
    assert status["data"]["reachable"] is True and status["meta"]["backend"] == "mock"

    server = ProfinetMCPServer(ProfinetConfig(backend="mock", mock_port=1, max_retries=0, timeout=1))
    status = await call(server, "get_connection_status")
    assert status["success"] is True and status["data"]["reachable"] is False


async def test_mock_backend_end_to_end_in_process(mock_server) -> None:
    config = ProfinetConfig(
        backend="mock", mock_port=mock_server.port, max_retries=0, writes_enabled=True, config_cmds_enabled=True
    )
    server = ProfinetMCPServer(config)
    assert isinstance(server.client.backend, MockBackend)
    found = await call(server, "discover_devices")
    assert found["data"]["devices"][0]["vendor"] == "MockVendor"
    wrote = await call(server, "write_io_data", device_name="PN-DEVICE-01", slot=1, subslot=2, data=[4, 3, 2, 1])
    assert wrote["success"] is True
    back = await call(server, "read_io_data", device_name="PN-DEVICE-01", slot=1, subslot=2, data_length=4)
    assert back["data"]["raw_data_hex"] == "04030201"
    refused = await call(server, "write_io_data", device_name="PN-DEVICE-01", slot=1, subslot=1, data=[1])
    assert refused["success"] is False and "input area" in refused["error"]


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_io_data", {"device_name": "pn-device-01", "slot": 1, "subslot": 2, "data": [1]}),
        ("write_device_by_alias", {"alias": "outputs", "data": [1]}),
        ("set_device_name", {"device_mac": MAC, "name": "renamed"}),
        ("set_device_ip", {"device_mac": MAC, "ip_address": "10.0.0.2", "subnet_mask": "255.0.0.0"}),
        ("identify_device", {"device_mac": MAC}),
    ],
)
async def test_lost_reply_to_a_change_is_reported_not_retried(tmp_path, tool: str, arguments: dict) -> None:
    """The mock applies the change and drops the reply: one request, success=false, outcome unknown."""
    received: list[dict] = []

    async def apply_then_hang_up(stream) -> None:
        async with stream:
            received.append(json.loads(await stream.receive()))

    device_map = tmp_path / "devices.json"
    device_map.write_text(json.dumps({"outputs": {"device_name": "pn-device-01", "slot": 1, "subslot": 2}}))
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    config = ProfinetConfig(
        backend="mock",
        mock_port=port,
        max_retries=3,
        retry_backoff_base=0.01,
        timeout=1,
        writes_enabled=True,
        config_cmds_enabled=True,
        device_map_path=device_map,
    )
    server = ProfinetMCPServer(config)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, apply_then_hang_up)
        try:
            result = await call(server, tool, **arguments)
        finally:
            tg.cancel_scope.cancel()
    assert result["success"] is False
    assert "the change may have been applied" in result["error"]
    assert result["meta"]["outcome_unknown"] is True and result["meta"]["backend"] == "mock"
    assert len(received) == 1
