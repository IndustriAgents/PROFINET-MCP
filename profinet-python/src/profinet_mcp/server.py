"""FastMCP server wiring for the PROFINET tools."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ContentBlock
from pydantic import ValidationError

from . import __version__
from .config import ProfinetConfig
from .gsd_parser import GSDParser
from .pn_client import ProfinetClient
from .tools import DeviceMap, ToolResources, invalid_arguments, register_tools

INSTRUCTIONS = """\
PROFINET IO tools. Every tool returns {success, data, error, meta}; check `success` and read `error`.
meta.backend says what answered: "mock" (profinet-mock-server, a simulator) or "network" (the real
PROFINET network, where only DCP discovery is implemented). Writes and DCP configuration commands are
disabled unless the operator enabled them; a refused call sends nothing. Call ping first to see the
backend and which switches are on."""


class EnvelopeFastMCP(FastMCP):
    """FastMCP that reports argument-validation failures as ``success: false`` envelopes.

    FastMCP validates arguments against each tool's schema before the tool runs and turns a failure
    into a protocol-level error. Here the model gets the usual envelope instead, naming each bad
    argument.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            tool = self._tool_manager.get_tool(name)
            if tool is None or not isinstance(exc.__cause__, ValidationError):
                raise
            return tool.fn_metadata.convert_result(invalid_arguments(name, exc.__cause__))


class ProfinetMCPServer:
    """Builds the FastMCP server from one :class:`ProfinetConfig`."""

    def __init__(self, config: ProfinetConfig | None = None, client: ProfinetClient | None = None) -> None:
        if client is not None:
            config = client.config
        self.config = config if config is not None else ProfinetConfig.from_env()
        self.client = client if client is not None else ProfinetClient(self.config)
        self.device_map = DeviceMap(self.config.device_map_path)
        self.gsd_parser = GSDParser(self.config.gsd_base_path)
        self.mcp = EnvelopeFastMCP(name="PROFINET MCP Server", instructions=INSTRUCTIONS)
        # FastMCP has no version argument; without this, serverInfo.version is the mcp SDK's version.
        self.mcp._mcp_server.version = __version__
        register_tools(
            self.mcp,
            ToolResources(
                client=self.client,
                config=self.config,
                device_map=self.device_map,
                gsd_parser=self.gsd_parser,
            ),
        )

    def run(self) -> None:
        """Serve MCP over stdio until the client disconnects."""
        self.mcp.run()
