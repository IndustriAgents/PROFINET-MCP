"""FastMCP server wiring for the PROFINET tools."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.stdio import stdio_server
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
PROFINET network, supporting DCP), or "cifx" (precommissioned controller process-image I/O). Writes and DCP commands are
disabled unless the operator enabled them; a refused call sends nothing. Tools that change a device
report meta.outcome (written, rejected, unknown, not_sent); after "unknown" the change may have been
applied, so read the device back before retrying. Call ping first to see the backend and switches."""


log = logging.getLogger(__name__)

#: Errors from writing a reply to a client that has disconnected.
_UNDELIVERABLE = (anyio.ClosedResourceError, anyio.BrokenResourceError, BrokenPipeError, ConnectionResetError)


class EnvelopeFastMCP(FastMCP):
    """FastMCP that reports argument-validation failures as ``success: false`` envelopes.

    FastMCP validates arguments against each tool's schema before the tool runs and turns a failure
    into a protocol-level error. Here the model gets the usual envelope instead, naming each bad
    argument.
    """

    #: Called once when the client's stdin reaches end of file.
    on_client_disconnect: Callable[[], None] | None = None
    #: True once the client's stdin has reached end of file.
    client_gone: bool = False

    async def run_stdio_async(self) -> None:
        """Serve stdio, and stop pending device requests as soon as the client disconnects.

        FastMCP stops reading at EOF but lets in-flight tool calls finish, so a write still connecting
        or waiting to retry would be sent after the client is gone. Messages are relayed through a
        stream whose end triggers ``on_client_disconnect`` first.
        """
        self.client_gone = False
        try:
            async with stdio_server() as (read_stream, write_stream):
                relay_send, relay_receive = anyio.create_memory_object_stream(0)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(self._relay_until_eof, read_stream, relay_send)
                    await self._mcp_server.run(
                        relay_receive, write_stream, self._mcp_server.create_initialization_options()
                    )
                    tg.cancel_scope.cancel()
        except BaseExceptionGroup as group:
            # Replies to calls that were still running cannot be delivered once the client is gone;
            # that is a normal end, not a crash.
            _, rest = group.split(_UNDELIVERABLE)
            if not self.client_gone or rest is not None:
                raise
            log.info("Dropped replies for the disconnected client")

    async def _relay_until_eof(self, source: Any, sink: Any) -> None:
        try:
            async with sink:
                async for message in source:
                    await sink.send(message)
        finally:
            self.client_gone = True
            log.info("MCP client disconnected; stopping pending device requests")
            if self.on_client_disconnect is not None:
                self.on_client_disconnect()

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Sequence[ContentBlock] | dict[str, Any]:
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            try:
                # Only validation performed before invocation proves not_sent.
                # A handler or result validator can fail after native dispatch.
                tool.fn_metadata.arg_model.model_validate(tool.fn_metadata.pre_parse_json(arguments))
            except ValidationError as exc:
                return tool.fn_metadata.convert_result(invalid_arguments(name, exc))
        return await super().call_tool(name, arguments)


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
        self.mcp.on_client_disconnect = self.client.shutdown
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
