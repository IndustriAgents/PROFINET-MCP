from __future__ import annotations

from dataclasses import dataclass

from profinet_mcp.config import ProfinetConfig
from profinet_mcp.domain.ports import ProfinetClientPort
from profinet_mcp.gsd_parser import GSDParser
from profinet_mcp.infrastructure.maps import DeviceMap


@dataclass(slots=True)
class ToolResources:
    client: ProfinetClientPort
    config: ProfinetConfig
    device_map: DeviceMap
    gsd_parser: GSDParser
