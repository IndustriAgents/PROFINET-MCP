from __future__ import annotations

from profinet_mcp.domain.model import DiscoveryTimeout, Envelope


class DiscoveryOperations:
    async def discover_devices(self, timeout: DiscoveryTimeout = None) -> Envelope:
        """Discover PROFINET IO devices."""
        devices, meta = await self.client.discover_devices(timeout)
        return self.ok("discover_devices", {"devices": devices, "count": len(devices)}, meta)
