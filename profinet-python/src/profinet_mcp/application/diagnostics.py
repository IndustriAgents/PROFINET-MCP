from __future__ import annotations

from profinet_mcp import __version__, validation
from profinet_mcp.backends import OperationRefused
from profinet_mcp.domain.errors import config_cmds_refusal
from profinet_mcp.domain.model import DeviceMac, DeviceRef, Envelope, IdentifySeconds


class DiagnosticsOperations:
    async def get_device_info(self, device_name: DeviceRef) -> Envelope:
        """Discovery details for one device, looked up by station name, IP address or MAC address."""
        wanted = validation.device_identifier(device_name)
        device, meta = await self.client.find_device(wanted)
        if device is None:
            return self.err("get_device_info", f"No device named or addressed {wanted!r} answered discovery", meta)
        return self.ok("get_device_info", device, meta)

    async def test_device_communication(self, device_name: DeviceRef) -> Envelope:
        """Check that a device answers, by station name, IP address or MAC address."""
        wanted = validation.device_identifier(device_name)
        device, meta = await self.client.find_device(wanted)
        if device is None:
            return self.err(
                "test_device_communication", f"Device {wanted!r} did not answer", {**meta, "reachable": False}
            )
        return self.ok(
            "test_device_communication",
            {"device_name": device.get("device_name"), "reachable": True, "device": device},
            meta,
        )

    async def identify_device(self, device_mac: DeviceMac, duration_s: IdentifySeconds = 5) -> Envelope:
        """Make a device flash its identification LED (DCP Set Signal) so it can be found in the cabinet."""
        if refusal := config_cmds_refusal(self.config):
            raise OperationRefused(refusal)
        mac = validation.mac_address(device_mac)
        result, meta = await self.client.identify_device(mac, duration_s)
        return self.ok("identify_device", result, meta)

    async def ping(self) -> Envelope:
        """Server health and settings: version, backend, connection settings and safety switches."""
        return self.ok(
            "ping",
            {
                "server": "profinet-mcp",
                "version": __version__,
                "backend": self.client.backend_name,
                "connection": self.client.connection_status(),
                "writes_enabled": self.config.writes_enabled,
                "config_cmds_enabled": self.config.config_cmds_enabled,
                "device_aliases": self.resources.device_map.count(),
                "device_map_error": self.resources.device_map.error,
            },
            self.backend_meta(contacted=False),
        )

    async def get_connection_status(self) -> Envelope:
        """Connection settings plus a reachability check of the backend."""
        status = self.client.connection_status()
        probe = await self.client.probe()
        status.update(reachable=probe.get("reachable"), detail=probe.get("detail"))
        return self.ok("get_connection_status", status, self.backend_meta())
