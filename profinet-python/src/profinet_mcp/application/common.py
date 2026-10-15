from __future__ import annotations

from typing import Any

from profinet_mcp import validation
from profinet_mcp.domain.model import Envelope, envelope


class CommonOperations:
    def __init__(self, resources, cancel_scope):
        self.resources = resources
        self.cancel_scope = cancel_scope
        self.client = self.resources.client
        self.config = self.resources.config

    def ok(self, tool: str, data: Any, meta: dict[str, Any] | None = None) -> Envelope:
        return envelope(True, data=data, meta={"tool": tool, **(meta or {})})

    def err(self, tool: str, message: str, meta: dict[str, Any] | None = None) -> Envelope:
        return envelope(False, error=message, meta={"tool": tool, **(meta or {})})

    def backend_meta(self, **extra: Any) -> dict[str, Any]:
        return {"backend": self.client.backend_name, **extra}

    def alias_spec(self, alias: str) -> tuple[str, dict[str, Any]]:
        spec = self.resources.device_map.get(alias)
        if spec is None:
            known = ", ".join(sorted(a["alias"] for a in self.resources.device_map.list())) or "none"
            hint = f" ({self.resources.device_map.error})" if self.resources.device_map.error else ""
            raise validation.InvalidArgument(f"unknown alias {alias!r}; known aliases: {known}{hint}")
        if not isinstance(spec, dict):
            raise validation.InvalidArgument(f"alias {alias!r} in DEVICE_MAP_FILE is not a JSON object")
        device_name = spec.get("device_name") or alias
        return (validation.device_identifier(str(device_name), f"alias {alias!r} device_name"), spec)
