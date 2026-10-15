from __future__ import annotations

try:
    from mcp.server.lowlevel.server import request_ctx as _request_ctx
except ImportError:
    _request_ctx = None
from typing import Any as Any

from mcp.server.fastmcp import FastMCP as FastMCP
from mcp.types import ToolAnnotations as ToolAnnotations

from profinet_mcp.protocol.support import _request_ctx as _request_ctx

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True)
LOCAL = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def request_cancel_scope() -> Any | None:
    """The MCP SDK's cancel scope for the request being handled, or None outside a request.

    Read through the session's in-flight responder (``session._in_flight[request_id]._cancel_scope``), a
    private mcp<2 API: guarded here, pinned by ``mcp<2``, and covered by a test that fails if it moves.
    """
    if _request_ctx is None:
        return None
    try:
        context = _request_ctx.get()
    except LookupError:
        return None
    in_flight = getattr(context.session, "_in_flight", None)
    responder = in_flight.get(context.request_id) if isinstance(in_flight, dict) else None
    return getattr(responder, "_cancel_scope", None)
