from __future__ import annotations

from typing import Any

from profinet_mcp import validation
from profinet_mcp.domain.model import Alias, DataLength, DeviceName, Envelope, Slot, Subslot


class ReadOperations:
    async def read(
        self, tool_name: str, device_name: str, slot: int, subslot: int, length: int, **extra: Any
    ) -> Envelope:
        device_name = validation.device_identifier(device_name)
        payload, meta = await self.client.read_io_data(device_name, slot, subslot, length)
        data = {
            **extra,
            "device_name": device_name,
            "slot": slot,
            "subslot": subslot,
            "data_length": len(payload),
            "raw_data_hex": payload.hex(),
            "bytes": list(payload),
        }
        return self.ok(tool_name, data, meta)

    async def read_io_data(
        self, device_name: DeviceName, slot: Slot, subslot: Subslot, data_length: DataLength
    ) -> Envelope:
        """Read I/O data from a slot/subslot of a device; returns raw_data_hex and the byte values."""
        return await self.read("read_io_data", device_name, slot, subslot, data_length)

    async def read_device_by_alias(self, alias: Alias) -> Envelope:
        """read_io_data for the device, slot, subslot and length behind an alias from DEVICE_MAP_FILE."""
        device_name, spec = self.alias_spec(alias)
        source = f"alias {alias!r}"
        slot, subslot = validation.io_address(spec.get("slot"), spec.get("subslot", 1), source=source)
        length = validation.data_length(spec.get("data_length", 8), source=source)
        return await self.read("read_device_by_alias", device_name, slot, subslot, length, alias=alias)
