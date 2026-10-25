"""Static backend support; never a probe or an authorization decision."""

from __future__ import annotations

from typing import Any


def capabilities(
    backend: str, *, writes_enabled: bool, extra_gate: bool = False, cifx_init_enabled: bool = False
) -> dict[str, Any]:
    operations: dict[str, dict[str, Any]] = {}
    for name in (
        "read_io_data",
        "write_io_data",
        "discover_devices",
        "get_device_info",
        "identify_device",
        "set_device_name",
        "set_device_ip",
        "list_devices",
        "load_gsd_file",
        "ping",
        "get_connection_status",
    ):
        supported = backend in ("mock", "network") or name in (
            "list_devices",
            "load_gsd_file",
            "ping",
            "get_connection_status",
        )
        reason = None
        if backend == "network" and name in ("read_io_data", "write_io_data"):
            supported = False
            reason = "No IO-controller stack"
        if backend == "cifx" and name in ("read_io_data", "write_io_data"):
            supported = True
        if not supported and reason is None:
            reason = "No supported native backend for this operation"
        enabled = supported
        if name in ("write_io_data",):
            enabled = supported and writes_enabled
        if backend == "cifx" and name in ("read_io_data", "write_io_data"):
            enabled = enabled and cifx_init_enabled
        if name in ("identify_device", "set_device_name", "set_device_ip"):
            enabled = supported and writes_enabled and extra_gate
        if supported and not enabled:
            reason = "Disabled by native configuration gates"
        operations[name] = {"supported": supported, "enabled": enabled}
        if reason is not None:
            operations[name]["reason"] = reason
    return {
        "protocol": "profinet",
        "backend": backend,
        "simulated": backend == "mock",
        "scope": "backend_support",
        "device_verified": False,
        "operations": operations,
        "prerequisites": [
            "Native configuration gates do not grant MCP policy or device permission.",
            "Live DCP discovery requires a configured interface and raw Ethernet privileges.",
            "Live cyclic I/O needs an IO-controller stack and an application relationship.",
            "cifx requires an explicitly enabled, exclusively owned UIO card in donttouch mode,",
            "commissioned PROFINET IO Controller V3.2 firmware and an exact process-image map.",
            "cifx checks a new buffered input handshake, AR state and IOPS/IOCS before mapped I/O.",
            "cifx write confirmation covers the controller process image, not physical actuation.",
            "DCP Set requires both configuration gates; live Signal accepts duration_s=3 only.",
        ],
    }
