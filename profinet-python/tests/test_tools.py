"""Tool behaviour through FastMCP: descriptions, envelopes, validation, safety switches, honesty."""

from __future__ import annotations

import json
import time
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
from profinet_mcp.tools import CHANGE_TOOLS

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

    def deadline(self, timeout=None) -> float:
        return 5.0

    async def discover(self, timeout, *, allow_cached=False):
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

    def shutdown(self) -> None:
        self.calls.append(("shutdown", ()))

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
    for tool in tools:  # rule 13: nothing that changes a device invites retries
        hints = tool.annotations
        if tool.name in CHANGE_TOOLS:
            assert (hints.readOnlyHint, hints.destructiveHint, hints.idempotentHint) == (False, True, False), tool.name
        else:
            assert hints.readOnlyHint is True, tool.name


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
    server, backend = make_server(writes_enabled=True, config_cmds_enabled=True)
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
        assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == ("not_sent", False)
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


CONFIG_COMMANDS = [
    ("set_device_name", {"device_mac": MAC, "name": "new-name"}),
    ("set_device_ip", {"device_mac": MAC, "ip_address": "10.0.0.2", "subnet_mask": "255.255.255.0"}),
    ("identify_device", {"device_mac": MAC}),
]


@pytest.mark.parametrize(
    ("switches", "missing"),
    [
        ({"writes_enabled": True}, "PROFINET_CONFIG_CMDS_ENABLED"),
        ({"config_cmds_enabled": True}, "PROFINET_WRITES_ENABLED"),
    ],
    ids=["writes-only", "config-only"],
)
async def test_config_commands_need_both_switches(switches: dict, missing: str) -> None:
    server, backend = make_server(**switches)
    for tool, args in CONFIG_COMMANDS:
        result = await call(server, tool, **args)
        assert result["success"] is False, tool
        assert f"not set: {missing}." in result["error"], result["error"]
        assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == ("not_sent", False)
    client = ProfinetClient(ProfinetConfig(backend="mock", **switches), backend)
    from profinet_mcp.pn_client import OperationRefused

    for call_client in (
        lambda: client.set_device_name(MAC, "new-name"),
        lambda: client.set_device_ip(MAC, "10.0.0.2", "255.255.255.0", "0.0.0.0"),
        lambda: client.identify_device(MAC, 5),
    ):
        with pytest.raises(OperationRefused, match=missing):
            await call_client()
    assert backend.calls == []


async def test_client_refuses_writes_on_its_own() -> None:
    """Defence in depth: the client checks PROFINET_WRITES_ENABLED even if a tool forgot to."""
    from profinet_mcp.pn_client import OperationRefused

    backend = RecordingBackend()
    client = ProfinetClient(ProfinetConfig(backend="mock", config_cmds_enabled=True), backend)
    with pytest.raises(OperationRefused, match="PROFINET_WRITES_ENABLED"):
        await client.write_io_data("x", 1, 2, b"\x01")
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
    assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == ("written", True)
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
    server, backend = make_server(writes_enabled=True, config_cmds_enabled=True)
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
    server, backend = make_server(writes_enabled=True, config_cmds_enabled=True)
    result = await call(server, "set_device_ip", device_mac=MAC, **args)
    assert result["success"] is False and fragment in result["error"]
    assert backend.calls == []


# -- reads and discovery --------------------------------------------------------------------


async def test_read_and_discovery_tools() -> None:
    server, backend = make_server(writes_enabled=True, config_cmds_enabled=True)
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
    server = ProfinetMCPServer(ProfinetConfig(writes_enabled=True, config_cmds_enabled=True))
    for tool, args in [
        ("discover_devices", {}),
        ("get_device_info", {"device_name": "pn-device-01"}),
        ("read_io_data", {"device_name": "pn-device-01", "slot": 1, "subslot": 1, "data_length": 8}),
        ("test_device_communication", {"device_name": "pn-device-01"}),
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
        expected = {"tool": tool, "backend": "network", "not_implemented": True}
        if tool != "read_io_data":  # tools that change a device also report the outcome
            expected.update(outcome="not_sent", request_sent=False)
        assert result["meta"] == expected
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
                "no_slot": {"device_name": "line1-io", "data_length": 2},
                "not_an_object": 5,
            }
        )
    )
    server, backend = make_server(writes_enabled=True, device_map_path=device_map)

    listed = await call(server, "list_devices")
    assert listed["success"] is True
    assert [d["alias"] for d in listed["data"]["devices"]] == [
        "line1_inputs",
        "line1_outputs",
        "broken_slot",
        "no_slot",
    ]
    assert listed["data"]["devices"][3]["slot"] is None
    assert "not_an_object" in listed["data"]["warning"]

    result = await call(server, "read_device_by_alias", alias="line1_inputs")
    assert result["success"] is True and result["data"]["alias"] == "line1_inputs"
    assert result["data"]["raw_data_hex"] == "00010203"

    result = await call(server, "write_device_by_alias", alias="line1_outputs", data=[7, 8])
    assert result["success"] is True and backend.calls[-1] == ("write_io", ("line1-io", 1, 2, b"\x07\x08"))

    result = await call(server, "read_device_by_alias", alias="broken_slot")
    assert result["success"] is False and "slot must be an integer" in result["error"]
    result = await call(server, "read_device_by_alias", alias="no_slot")
    assert result["success"] is False and "no slot given" in result["error"]
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
    assert result["data"]["module_count"] == 5
    assert [sub["id"] for sub in result["data"]["submodules"]] == ["IDSM_PLUG_DI16", "IDSM_PLUG_DO16"]
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
    wrote = await call(server, "write_io_data", device_name="pn-device-01", slot=1, subslot=2, data=[4, 3, 2, 1])
    assert wrote["success"] is True
    back = await call(server, "read_io_data", device_name="pn-device-01", slot=1, subslot=2, data_length=4)
    assert back["data"]["raw_data_hex"] == "04030201"
    refused = await call(server, "write_io_data", device_name="pn-device-01", slot=1, subslot=1, data=[1])
    assert refused["success"] is False and "input area" in refused["error"]
    assert (refused["meta"]["outcome"], refused["meta"]["request_sent"]) == ("rejected", True)
    assert "outcome" not in back["meta"]  # only tools that change a device report an outcome


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
    assert "may have been applied; check before retrying" in result["error"]
    assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == ("unknown", True)
    assert result["meta"]["outcome_unknown"] is True and result["meta"]["backend"] == "mock"
    assert len(received) == 1


async def test_outcome_is_not_sent_when_nothing_went_out() -> None:
    server, _ = make_server(writes_enabled=True)
    invalid = await call(server, "write_io_data", device_name="x", slot=-1, subslot=1, data=[1])
    assert (invalid["meta"]["outcome"], invalid["meta"]["request_sent"]) == ("not_sent", False)

    unreachable = ProfinetMCPServer(
        ProfinetConfig(backend="mock", mock_port=1, max_retries=0, timeout=1, writes_enabled=True)
    )
    result = await call(unreachable, "write_io_data", device_name="x", slot=1, subslot=2, data=[1])
    assert result["success"] is False and "Cannot reach the mock" in result["error"]
    assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == ("not_sent", False)

    nothing = ProfinetMCPServer(ProfinetConfig(writes_enabled=True))
    result = await call(nothing, "write_io_data", device_name="x", slot=1, subslot=2, data=[1])
    assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == ("not_sent", False)


@pytest.mark.parametrize(
    ("reply", "outcome"),
    [
        (b"not json\n", "unknown"),
        (b'{"data": 1}\n', "unknown"),
        (b'{"success": false, "error": "Internal mock error: boom"}\n', "unknown"),
        (b'{"success": false, "error": "Slot 1/subslot 2 not found", "rejected": true}\n', "rejected"),
        (b'{"success": false, "error": "odd", "rejected": "true"}\n', "unknown"),
        (b'{"success": false, "error": "odd", "rejected": 1}\n', "unknown"),
    ],
    ids=["garbage", "no-success-flag", "internal-error", "explicit-refusal", "string-true", "integer-1"],
)
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("write_io_data", {"device_name": "pn-device-01", "slot": 1, "subslot": 2, "data": [1]}),
        ("set_device_name", {"device_mac": MAC, "name": "renamed"}),
        ("set_device_ip", {"device_mac": MAC, "ip_address": "10.0.0.2", "subnet_mask": "255.0.0.0"}),
        ("identify_device", {"device_mac": MAC}),
    ],
)
async def test_reply_classification_for_changes(tool: str, arguments: dict, reply: bytes, outcome: str) -> None:
    received: list[dict] = []

    async def answer(stream) -> None:
        async with stream:
            received.append(json.loads(await stream.receive()))
            await stream.send(reply)

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    config = ProfinetConfig(
        backend="mock",
        mock_port=listener.extra(SocketAttribute.local_port),
        max_retries=3,
        retry_backoff_base=0.01,
        timeout=1,
        writes_enabled=True,
        config_cmds_enabled=True,
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, answer)
        try:
            result = await call(ProfinetMCPServer(config), tool, **arguments)
        finally:
            tg.cancel_scope.cancel()
    assert result["success"] is False
    assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == (outcome, True)
    if outcome == "unknown":
        assert "may have been applied; check before retrying" in result["error"]
    assert len(received) == 1


class HangingBackend(RecordingBackend):
    """Never answers: the client's overall deadline must end every call (rule 12)."""

    def deadline(self, timeout=None) -> float:
        return 0.3

    async def _call(self, op: str, *args: Any) -> None:
        self.calls.append((op, args))
        await anyio.sleep(30)


async def test_overall_deadline_ends_a_hung_call() -> None:
    server, backend = make_server(HangingBackend(), writes_enabled=True, config_cmds_enabled=True)
    with anyio.fail_after(10):
        wrote = await call(server, "write_io_data", device_name="x", slot=1, subslot=2, data=[1])
        read = await call(server, "read_io_data", device_name="x", slot=1, subslot=1, data_length=1)
        named = await call(server, "set_device_name", device_mac=MAC, name="late")
    assert wrote["success"] is False and (wrote["meta"]["outcome"], wrote["meta"]["request_sent"]) == ("unknown", True)
    assert "may have been applied" in wrote["error"]
    assert named["meta"]["outcome"] == "unknown"
    assert read["success"] is False and "deadline" in read["error"] and "outcome" not in read["meta"]


async def test_local_failure_before_sending_is_not_sent() -> None:
    from profinet_mcp.backends import RequestNotSent

    server, _ = make_server(
        RecordingBackend(fail=RequestNotSent("Could not encode the 'write' request")), writes_enabled=True
    )
    result = await call(server, "write_io_data", device_name="x", slot=1, subslot=2, data=[1])
    assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == ("not_sent", False)


async def test_lookups_after_discovery_use_the_cached_answer(monkeypatch) -> None:
    from test_dcp import XID, identify_response

    from profinet_mcp import dcp

    monkeypatch.setattr(dcp.random, "getrandbits", lambda bits: XID)
    transport = FakeTransport([identify_response(xid=XID)])
    server, _ = make_server(NetworkBackend("fake0", transport=transport))
    found = await call(server, "discover_devices", timeout=0.5)
    assert found["success"] is True and found["meta"]["cached"] is False
    info = await call(server, "get_device_info", device_name="line1-io")
    assert info["success"] is True and info["meta"]["cached"] is True and info["meta"]["age_s"] >= 0
    reach = await call(server, "test_device_communication", device_name="line1-io")
    assert reach["success"] is True and reach["meta"]["cached"] is True
    assert len(transport.sent) == 1
    again = await call(server, "discover_devices", timeout=0.5)
    assert again["success"] is False and again["meta"]["refused"] is True


class OneClientAtATimeDevice:
    """A device that serves one connection at a time, like a PLC with a busy accept queue.

    Every request is recorded with its arrival time; reads take ``busy_s`` to process. While the
    device is busy (or other requests wait for it), new connections are not accepted.
    """

    def __init__(self, busy_s: float) -> None:
        self.busy_s = busy_s
        self.slot = anyio.Lock()
        self.received: list[tuple[str, float]] = []
        self.started = time.monotonic()
        self.connect_attempts: list[str] = []

    async def handle(self, stream) -> None:
        async with stream, self.slot:
            try:
                request = json.loads(await stream.receive())
            except (anyio.EndOfStream, anyio.BrokenResourceError, OSError):
                return
            self.received.append((request["op"], round(time.monotonic() - self.started, 2)))
            await anyio.sleep(self.busy_s)
            reply = {"success": True, "data": {"device_name": "x", "raw_data_hex": "00"}}
            try:
                await stream.send(json.dumps(reply).encode() + b"\n")
            except (anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                pass  # the caller gave up long ago

    def busy(self) -> bool:
        return self.slot.locked() or self.slot.statistics().tasks_waiting > 0


async def test_a_change_queued_behind_slow_calls_is_never_sent_after_its_deadline(monkeypatch) -> None:
    """Rules 1/12: the deadline counts from the start of the tool call and covers any wait to reach the
    device. A write whose deadline expires while it waits is not_sent, and it must never be sent later -
    the Python MCP SDK sends no notifications/cancelled when its own client-side timeout fires."""
    device = OneClientAtATimeDevice(busy_s=1.2)
    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    real_connect = anyio.connect_tcp

    async def connect_when_device_is_free(host, port_, **kwargs):
        device.connect_attempts.append("connect")
        while device.busy():  # the device is not accepting: the caller waits, queued behind the reads
            await anyio.sleep(0.01)
        return await real_connect(host, port_, **kwargs)

    config = ProfinetConfig(
        backend="mock", mock_port=port, timeout=1, max_retries=0, writes_enabled=True, config_cmds_enabled=True
    )
    server = ProfinetMCPServer(config)
    deadline = server.client.backend.deadline(None)
    results: dict = {}

    async def run(name: str, tool: str, arguments: dict) -> None:
        results[name] = (await call(server, tool, **arguments), time.monotonic() - device.started)

    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, device.handle)
        for n in range(3):  # three slow reads occupy the device for ~3.6 s
            tg.start_soon(
                run, f"read{n}", "read_io_data", {"device_name": "x", "slot": 1, "subslot": 1, "data_length": 1}
            )
        await anyio.sleep(0.2)
        monkeypatch.setattr(anyio, "connect_tcp", connect_when_device_is_free)
        tg.start_soon(run, "write", "write_io_data", {"device_name": "x", "slot": 1, "subslot": 2, "data": [7]})
        await anyio.sleep(3 * device.busy_s + 1.5)  # well past the time the device is free again
        tg.cancel_scope.cancel()

    write, finished_at = results["write"]
    assert device.connect_attempts, "the write never tried to reach the device"
    assert write["success"] is False
    assert (write["meta"]["outcome"], write["meta"]["request_sent"]) == ("not_sent", False)
    assert finished_at < 0.2 + deadline + 0.5  # answered within its own deadline
    assert [op for op, _ in device.received].count("write") == 0, device.received  # and never sent afterwards
    assert [op for op, _ in device.received].count("read") == 3


async def test_deadline_already_spent_before_the_backend_is_reached() -> None:
    """The deadline counts from the start of the tool call, not from the start of the device request."""
    from profinet_mcp import backends

    received: list[bytes] = []

    async def record(stream) -> None:
        async with stream:
            received.append(await stream.receive())

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    backend = backends.MockBackend("127.0.0.1", listener.extra(SocketAttribute.local_port), timeout=1, max_retries=0)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, record)
        with backends.tool_call():
            backends._CALL.get().started -= 3600  # this tool call started an hour ago
            with pytest.raises(backends.RequestNotSent, match="deadline .* had already expired"):
                await backend.write_io("x", 1, 2, b"\x01")
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    assert received == []


async def test_one_deadline_per_tool_call_is_shared_by_all_its_requests() -> None:
    """A second request in the same tool call gets only what is left; it is never given a fresh deadline."""
    from profinet_mcp import backends

    received: list[str] = []

    async def answer(stream) -> None:
        async with stream:
            request = json.loads(await stream.receive())
            received.append(request["op"])
            await anyio.sleep(0.8)  # a slow, but in-time, reply
            reply = {"success": True, "data": {"device_name": "x", "raw_data_hex": "00", "written_bytes": 1}}
            await stream.send(json.dumps(reply).encode() + b"\n")

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    backend = backends.MockBackend("127.0.0.1", listener.extra(SocketAttribute.local_port), timeout=1, max_retries=0)
    bound = backend.deadline(None)  # 3 s for this configuration
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, answer)
        with backends.tool_call():
            await backend.read_io("x", 1, 1, 1)  # ~0.8 s of the call's 3 s
            not_after = backends._CALL.get().not_after
            await anyio.sleep(bound - 0.8 + 0.2)  # more work in the same call: now past the call's deadline
            with pytest.raises(backends.RequestNotSent, match="deadline .* had already expired"):
                await backend.write_io("x", 1, 2, b"\x01")
            assert backends._CALL.get().not_after == not_after  # not restarted by the second request
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    assert received == ["read"]  # the write was never sent


async def test_a_send_after_the_call_deadline_is_refused_as_not_sent(monkeypatch) -> None:
    """Checked right before the send: a connection that completes after the deadline sends nothing."""
    from profinet_mcp import backends

    received: list[bytes] = []

    async def record(stream) -> None:
        async with stream:
            try:
                received.append(await stream.receive())
            except anyio.EndOfStream:
                pass

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    port = listener.extra(SocketAttribute.local_port)
    backend = backends.MockBackend("127.0.0.1", port, timeout=5, max_retries=0)
    real_connect = anyio.connect_tcp

    async def slow_connect(*args, **kwargs):
        stream = await real_connect(*args, **kwargs)
        time.sleep(0.4)  # blocks without a checkpoint, so only the explicit pre-send check can notice
        return stream

    monkeypatch.setattr(anyio, "connect_tcp", slow_connect)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, record)
        with backends.tool_call():
            backends._CALL.get().not_after = time.monotonic() + 0.2  # this call has 0.2 s left
            with pytest.raises(backends.RequestNotSent, match="expired before it could be sent"):
                await backend.write_io("x", 1, 2, b"\x01")
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    assert received == []


@pytest.mark.parametrize("trigger", ["disconnect", "deadline"])
async def test_change_interrupted_mid_send_is_unknown(mock_server, monkeypatch, trigger: str) -> None:
    """Rule 1a: once part of a change has left, it may have been applied. A disconnect or the call's deadline
    hitting while send() is still writing must report "unknown" with request_sent true - never "not_sent"."""
    config = ProfinetConfig(backend="mock", mock_port=mock_server.port, max_retries=0, timeout=30, writes_enabled=True)
    server = ProfinetMCPServer(config)
    backend = server.client.backend
    real_connect = backend._connect
    partial = anyio.Event()

    async def connect_with_slow_send():
        stream = await real_connect()
        real_send = stream.send

        async def send(data: bytes) -> None:
            await real_send(data[:5])  # part of the request has left ...
            partial.set()
            await anyio.sleep(30)  # ... and the rest is stuck
            await real_send(data[5:])

        stream.send = send
        return stream

    monkeypatch.setattr(backend, "_connect", connect_with_slow_send)
    if trigger == "deadline":
        monkeypatch.setattr(backend, "deadline", lambda timeout=None: 0.6)
    results: dict = {}

    async def write() -> None:
        results["write"] = await call(server, "write_io_data", device_name="pn-device-01", slot=1, subslot=2, data=[5])

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(write)
            await partial.wait()
            if trigger == "disconnect":
                server.client.shutdown()
    result = results["write"]
    assert result["success"] is False
    assert (result["meta"]["outcome"], result["meta"]["request_sent"]) == ("unknown", True)
    assert "may have been applied" in result["error"]


class NoCheckpointStream:
    """Wraps a TCP stream so that send() writes in the current loop step, without a checkpoint.

    anyio's own send() happens to checkpoint first; transports that do not (threads, other stacks) rely on
    the explicit pre-send gate alone, which is what this lets the test isolate.
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.transport = inner._transport  # anyio's asyncio SocketStream; writes are queued immediately

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.inner.aclose()

    async def send(self, data: bytes) -> None:
        self.transport.write(data)  # no checkpoint: written in this very loop step

    async def receive(self, max_bytes: int = 65536) -> bytes:
        return await self.inner.receive(max_bytes)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def test_cancel_in_the_same_step_as_the_connect_never_sends(monkeypatch) -> None:
    """CANCEL RACE: the SDK sets the request scope's cancel_called synchronously, but anyio lets a task whose
    connect just completed run one more step. The pre-send gate must read cancel_called itself."""
    from profinet_mcp import backends

    received: list[bytes] = []

    async def record(stream) -> None:
        async with stream:
            try:
                received.append(await stream.receive())
            except (anyio.EndOfStream, anyio.BrokenResourceError):
                pass

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1", local_port=0)
    backend = backends.MockBackend("127.0.0.1", listener.extra(SocketAttribute.local_port), timeout=5, max_retries=0)
    real_connect = anyio.connect_tcp
    current: dict = {}

    async def connect_then_cancel(*args, **kwargs):
        stream = await real_connect(*args, **kwargs)
        current["connects"] = current.get("connects", 0) + 1
        current["scope"].cancel()  # like RequestResponder.cancel(): same loop step as the connect completing
        return NoCheckpointStream(stream)

    monkeypatch.setattr(anyio, "connect_tcp", connect_then_cancel)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, record)
        for _ in range(60):
            scope = anyio.CancelScope()
            current["scope"] = scope
            # The call ends either with ClientCancelled (not_sent) or, once asyncio delivers the pending
            # cancellation at a later await, with the cancellation itself - which the scope absorbs, just as
            # the SDK answers "Request cancelled". Either way nothing may reach the device.
            with scope, backends.tool_call(cancel_scope=scope):
                try:
                    await backend.write_io("x", 1, 2, b"\x01")
                except backends.ClientCancelled:
                    pass
        await anyio.sleep(0.3)
        tg.cancel_scope.cancel()
    assert current["connects"] == 60  # every iteration got as far as a live connection
    assert received == [], f"{len(received)} write(s) reached the device after the call was cancelled"


async def test_request_cancel_scope_is_the_sdks_and_sees_cancellation() -> None:
    """Fails if the private mcp<2 internals we read (request_ctx, session._in_flight, _cancel_scope) move."""
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.memory import create_connected_server_and_client_session
    from mcp.types import CancelledNotification, CancelledNotificationParams, ClientNotification

    from profinet_mcp import backends
    from profinet_mcp.tools import request_cancel_scope

    assert request_cancel_scope() is None  # outside a request
    seen: dict = {}
    reached = anyio.Event()

    class WaitingBackend(RecordingBackend):
        async def write_io(self, device, slot, subslot, data):
            context = request_ctx.get()
            seen["request_id"] = context.request_id
            seen["sdk_scope"] = context.session._in_flight[context.request_id]._cancel_scope
            seen["captured"] = backends._CALL.get().cancel_scope
            seen["cancelled_before"] = backends.call_cancelled()
            reached.set()
            try:
                await anyio.sleep(30)
            finally:
                seen["cancelled_after"] = backends.call_cancelled()
            return {"written_bytes": len(data)}, {"backend": self.name}

    server, _ = make_server(WaitingBackend(), writes_enabled=True)
    async with create_connected_server_and_client_session(server.mcp) as client:
        async with anyio.create_task_group() as tg:

            async def call_write() -> None:
                try:
                    await client.call_tool("write_io_data", {"device_name": "x", "slot": 1, "subslot": 2, "data": [1]})
                except Exception:  # noqa: BLE001 - the cancelled request may end with an error reply
                    pass

            tg.start_soon(call_write)
            with anyio.fail_after(10):
                await reached.wait()
            notification = CancelledNotification(
                params=CancelledNotificationParams(requestId=seen["request_id"], reason="test")
            )
            await client.send_notification(ClientNotification(notification))
            with anyio.fail_after(10):
                while "cancelled_after" not in seen:
                    await anyio.sleep(0.01)
            tg.cancel_scope.cancel()
    assert isinstance(seen["captured"], anyio.CancelScope)
    assert seen["captured"] is seen["sdk_scope"]
    assert seen["cancelled_before"] is False and seen["cancelled_after"] is True
