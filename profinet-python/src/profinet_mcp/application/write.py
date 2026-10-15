from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from profinet_mcp import validation
from profinet_mcp.backends import OperationRefused
from profinet_mcp.domain.errors import WRITES_DISABLED, config_cmds_refusal
from profinet_mcp.domain.model import (
    Alias,
    ByteValues,
    DeviceMac,
    DeviceName,
    Envelope,
    IPv4,
    Slot,
    StationName,
    Subslot,
)


class WriteOperations:
    async def write(
        self, tool_name: str, device_name: str, slot: int, subslot: int, data: list[int], **extra: Any
    ) -> Envelope:
        device_name = validation.device_identifier(device_name)
        payload = bytes(data)
        result, meta = await self.client.write_io_data(device_name, slot, subslot, payload)
        return self.ok(
            tool_name, {**extra, "device_name": device_name, "slot": slot, "subslot": subslot, **result}, meta
        )

    async def set_device_name(self, device_mac: DeviceMac, name: StationName) -> Envelope:
        """Assign a new station name (DCP Set NameOfStation) to the device with this MAC address."""
        if refusal := config_cmds_refusal(self.config):
            raise OperationRefused(refusal)
        mac = validation.mac_address(device_mac)
        new_name = validation.station_name(name)
        result, meta = await self.client.set_device_name(mac, new_name)
        return self.ok("set_device_name", result, meta)

    async def set_device_ip(
        self,
        device_mac: DeviceMac,
        ip_address: IPv4,
        subnet_mask: IPv4,
        gateway: Annotated[str | None, Field(description="Default gateway; omit or 0.0.0.0 for none")] = None,
    ) -> Envelope:
        """Assign IP address, subnet mask and gateway (DCP Set IP) to the device with this MAC address."""
        if refusal := config_cmds_refusal(self.config):
            raise OperationRefused(refusal)
        mac = validation.mac_address(device_mac)
        ip, mask, gw = validation.ip_settings(ip_address, subnet_mask, gateway)
        result, meta = await self.client.set_device_ip(mac, ip, mask, gw)
        return self.ok("set_device_ip", result, meta)

    async def write_io_data(self, device_name: DeviceName, slot: Slot, subslot: Subslot, data: ByteValues) -> Envelope:
        """Write bytes to an output slot/subslot of a device. This changes physical outputs."""
        if not self.config.writes_enabled:
            raise OperationRefused(WRITES_DISABLED)
        return await self.write("write_io_data", device_name, slot, subslot, data)

    async def write_device_by_alias(self, alias: Alias, data: ByteValues) -> Envelope:
        """write_io_data for the device, slot and subslot behind an alias from DEVICE_MAP_FILE."""
        if not self.config.writes_enabled:
            raise OperationRefused(WRITES_DISABLED)
        device_name, spec = self.alias_spec(alias)
        slot, subslot = validation.io_address(spec.get("slot"), spec.get("subslot", 1), source=f"alias {alias!r}")
        return await self.write("write_device_by_alias", device_name, slot, subslot, data, alias=alias)
