"""End to end: the mock IO device and the MCP server as subprocesses, driven over stdio.

The mock runs from ../profinet-mock-server on an OS-assigned port; the server is started the way an
MCP client starts it (``python -m profinet_mcp.cli``) and driven with the MCP client SDK.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from helpers import SAMPLE_GSDML, MockProcess, clean_env
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from profinet_mcp import __version__

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

MAC = "00:11:22:33:44:55"


@pytest.fixture
def mock_process(tmp_path: Path):
    process = MockProcess(tmp_path)
    try:
        yield process
    finally:
        process.stop()


@asynccontextmanager
async def mcp_session(cwd: Path, **env: str) -> AsyncIterator[tuple[ClientSession, Any]]:
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "profinet_mcp.cli"], env=clean_env(**env), cwd=str(cwd)
    )
    with open(cwd / "server-stderr.log", "w", encoding="utf-8") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                yield session, init


async def call(session: ClientSession, tool: str, **arguments: Any) -> dict[str, Any]:
    result = await session.call_tool(tool, arguments)
    assert not result.isError, result  # failures are envelopes, never protocol errors
    envelope = json.loads(result.content[0].text)
    assert result.structuredContent == envelope
    assert set(envelope) == {"success", "data", "error", "meta"}
    return envelope


async def test_every_tool_against_the_mock(tmp_path: Path, mock_process: MockProcess) -> None:
    device_map = tmp_path / "devices.json"
    device_map.write_text(
        json.dumps(
            {
                "inputs": {"device_name": "pn-device-01", "slot": 1, "subslot": 1, "data_length": 8},
                "outputs": {"device_name": "pn-device-01", "slot": 1, "subslot": 2, "data_length": 4},
            }
        )
    )
    before = mock_process.connections()
    with anyio.fail_after(60):
        async with mcp_session(
            tmp_path,
            PROFINET_BACKEND="mock",
            PROFINET_MOCK_PORT=str(mock_process.port),
            PROFINET_WRITES_ENABLED="true",
            PROFINET_CONFIG_CMDS_ENABLED="true",
            PROFINET_MAX_RETRIES="0",
            DEVICE_MAP_FILE=str(device_map),
        ) as (session, init):
            assert init.serverInfo.name == "PROFINET MCP Server"
            assert init.serverInfo.version == __version__

            tools = (await session.list_tools()).tools
            assert len(tools) == 14
            assert all(tool.description for tool in tools)

            ping = await call(session, "ping")
            assert ping["data"]["backend"] == "mock" and ping["data"]["writes_enabled"] is True

            status = await call(session, "get_connection_status")
            assert status["data"]["reachable"] is True

            found = await call(session, "discover_devices")
            assert found["success"] is True and found["meta"]["backend"] == "mock"
            (device,) = found["data"]["devices"]
            assert (device["device_name"], device["vendor"], device["mac_address"]) == (
                "pn-device-01",
                "MockVendor",
                MAC,
            )

            info = await call(session, "get_device_info", device_name="192.168.1.100")
            assert info["data"]["device_name"] == "pn-device-01"
            reach = await call(session, "test_device_communication", device_name="pn-device-01")
            assert reach["success"] is True and reach["data"]["reachable"] is True

            read = await call(session, "read_io_data", device_name="pn-device-01", slot=1, subslot=1, data_length=8)
            assert read["success"] is True and len(read["data"]["bytes"]) == 8 and read["meta"]["backend"] == "mock"

            wrote = await call(
                session, "write_io_data", device_name="pn-device-01", slot=1, subslot=2, data=[1, 2, 3, 4]
            )
            assert wrote["success"] is True and wrote["data"]["written_bytes"] == 4
            back = await call(session, "read_io_data", device_name="pn-device-01", slot=1, subslot=2, data_length=4)
            assert back["data"]["raw_data_hex"] == "01020304"

            aliases = await call(session, "list_devices")
            assert aliases["data"]["count"] == 2
            by_alias = await call(session, "write_device_by_alias", alias="outputs", data=[9, 9])
            assert by_alias["success"] is True
            by_alias = await call(session, "read_device_by_alias", alias="outputs")
            assert by_alias["data"]["raw_data_hex"] == "09090304"

            gsd = await call(session, "load_gsd_file", filepath=str(SAMPLE_GSDML))
            assert gsd["success"] is True and gsd["data"]["module_count"] == 4

            flash = await call(session, "identify_device", device_mac=MAC, duration_s=2)
            assert flash["success"] is True and flash["meta"]["backend"] == "mock"

            new_ip = await call(
                session, "set_device_ip", device_mac=MAC, ip_address="192.168.1.120", subnet_mask="255.255.255.0"
            )
            assert new_ip["success"] is True
            renamed = await call(session, "set_device_name", device_mac=MAC, name="line1-io")
            assert renamed["success"] is True and renamed["data"]["previous_name"] == "pn-device-01"
            info = await call(session, "get_device_info", device_name="line1-io")
            assert info["data"]["ip_address"] == "192.168.1.120"

            bad = await call(session, "read_io_data", device_name="line1-io", slot=-1, subslot=1, data_length=4)
            assert bad["success"] is False and bad["error"].startswith("Invalid arguments")
            missing = await call(session, "read_io_data", device_name="line1-io", slot=7, subslot=1, data_length=4)
            assert missing["success"] is False and "not found" in missing["error"]

    # The server really talked to the mock (the original smoke test counted zero connections): one TCP
    # connection per tool call that needs the device - 14 of the calls above.
    assert mock_process.wait_for_connections(before + 14) - before == 14


async def test_defaults_are_read_only(tmp_path: Path, mock_process: MockProcess) -> None:
    with anyio.fail_after(60):
        async with mcp_session(tmp_path, PROFINET_BACKEND="mock", PROFINET_MOCK_PORT=str(mock_process.port)) as (
            session,
            _,
        ):
            ping = await call(session, "ping")
            assert ping["data"]["writes_enabled"] is False and ping["data"]["config_cmds_enabled"] is False
            refused = await call(session, "write_io_data", device_name="pn-device-01", slot=1, subslot=2, data=[5])
            assert refused["success"] is False and "PROFINET_WRITES_ENABLED" in refused["error"]
            refused = await call(session, "set_device_name", device_mac=MAC, name="other")
            assert refused["success"] is False and "PROFINET_CONFIG_CMDS_ENABLED" in refused["error"]
            back = await call(session, "read_io_data", device_name="pn-device-01", slot=1, subslot=2, data_length=4)
            assert back["data"]["raw_data_hex"] == "00000000"  # nothing was written


class RawServer:
    """The server as a plain subprocess, to look at every byte it writes to stdout."""

    def __init__(self, cwd: Path, **env: str) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "profinet_mcp.cli"],
            cwd=cwd,
            env=clean_env(**env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.responses: queue.Queue[dict] = queue.Queue()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(self.proc.stderr), daemon=True).start()

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:
            self.stdout.append(line)
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict) and "id" in message:
                self.responses.put(message)

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, request_id: int, method: str, params: dict | None = None) -> dict:
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        response = self.responses.get(timeout=30)
        assert response["id"] == request_id, response
        return response

    def close(self) -> None:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=10)


@pytest.mark.parametrize("backend", ["mock", "network", "none"])
def test_stdout_carries_only_json_rpc(tmp_path: Path, mock_process: MockProcess, backend: str) -> None:
    server = RawServer(
        tmp_path, PROFINET_BACKEND=backend, PROFINET_MOCK_PORT=str(mock_process.port), PROFINET_INTERFACE="lo0"
    )
    try:
        init = server.request(
            1,
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
        )
        assert init["result"]["serverInfo"]["version"] == __version__
        server.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert len(server.request(2, "tools/list")["result"]["tools"]) == 14
        calls = [
            ("ping", {}),
            ("get_connection_status", {}),
            ("read_io_data", {"device_name": "pn-device-01", "slot": 1, "subslot": 1, "data_length": 8}),
            ("read_io_data", {"device_name": "pn-device-01", "slot": "x", "subslot": 1, "data_length": 8}),
            ("write_io_data", {"device_name": "pn-device-01", "slot": 1, "subslot": 2, "data": [1]}),
            ("load_gsd_file", {"filepath": str(SAMPLE_GSDML)}),
        ]
        if backend != "network":  # network discovery would open a raw socket; tests never do that
            calls.append(("discover_devices", {}))
        for offset, (name, arguments) in enumerate(calls, start=3):
            response = server.request(offset, "tools/call", {"name": name, "arguments": arguments})
            envelope = json.loads(response["result"]["content"][0]["text"])
            assert response["result"]["isError"] is False
            assert envelope["meta"]["tool"] == name
    finally:
        server.close()
    assert server.stdout, "no output at all"
    for line in server.stdout:
        message = json.loads(line)  # every stdout line is a JSON-RPC message
        assert message.get("jsonrpc") == "2.0", line
    assert any(f"backend={backend}" in line for line in server.stderr), "startup log should go to stderr"
