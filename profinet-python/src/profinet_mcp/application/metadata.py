from __future__ import annotations

from typing import Any

import anyio.lowlevel

from profinet_mcp.domain.model import Envelope, FilePath
from profinet_mcp.gsd_parser import GSDMLError


class MetadataOperations:
    async def list_devices(self) -> Envelope:
        """List the aliases defined in DEVICE_MAP_FILE (device name, address, slot, subslot, length)."""
        meta = {"source": "device_map", "device_map_file": str(self.resources.device_map.path or "") or None}
        entries = self.resources.device_map.list()
        if self.resources.device_map.error and (not entries):
            return self.err("list_devices", self.resources.device_map.error, meta)
        data: dict[str, Any] = {"devices": entries, "count": len(entries)}
        if self.resources.device_map.error:
            data["warning"] = self.resources.device_map.error
        return self.ok("list_devices", data, meta)

    async def load_gsd_file(self, filepath: FilePath) -> Envelope:
        """Parse a GSDML device description file: device identity, access points and modules."""
        meta: dict[str, Any] = {"source": "gsdml_file", "filepath": filepath}
        try:
            entry, cached = await anyio.to_thread.run_sync(self.resources.gsd_parser.load_cached, filepath)
        except FileNotFoundError as exc:
            return self.err("load_gsd_file", str(exc), meta)
        except GSDMLError as exc:
            return self.err("load_gsd_file", str(exc), meta)
        except OSError as exc:
            return self.err("load_gsd_file", f"Cannot read {filepath}: {exc.strerror or exc}", meta)
        meta["cached"] = cached
        return self.ok(
            "load_gsd_file",
            {
                "filepath": str(entry.path),
                "metadata": entry.metadata,
                "device_access_points": entry.device_access_points,
                "modules": entry.modules,
                "module_count": len(entry.modules),
                "submodules": entry.submodules,
            },
            meta,
        )
