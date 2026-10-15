from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DeviceMap:
    """Aliases from ``DEVICE_MAP_FILE``, re-read whenever the file changes."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.error: str | None = None
        self._devices: dict[str, Any] = {}
        self._stamp: tuple[int, int] | None = None
        self.refresh()

    def refresh(self) -> None:
        if not self.path:
            self._devices, self._stamp, self.error = ({}, None, None)
            return
        try:
            stat = self.path.stat()
        except OSError as exc:
            self._devices, self._stamp = ({}, None)
            self.error = f"DEVICE_MAP_FILE {self.path} cannot be read: {exc.strerror or exc}"
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._devices = {}
            self.error = f"DEVICE_MAP_FILE {self.path} is not valid JSON: {exc}"
            return
        if not isinstance(data, dict):
            self._devices = {}
            self.error = f"DEVICE_MAP_FILE {self.path} must hold a JSON object mapping aliases to devices"
            return
        self._devices = {str(name): spec for name, spec in data.items()}
        bad = sorted((name for name, spec in self._devices.items() if not isinstance(spec, dict)))
        self.error = f"DEVICE_MAP_FILE entries that are not JSON objects: {', '.join(bad)}" if bad else None

    def get(self, alias: str) -> Any:
        self.refresh()
        return self._devices.get(alias)

    def list(self) -> list[dict[str, Any]]:
        self.refresh()
        return [
            {
                "alias": alias,
                "device_name": spec.get("device_name"),
                "ip_address": spec.get("ip_address"),
                "description": spec.get("description"),
                "slot": spec.get("slot"),
                "subslot": spec.get("subslot", 1),
                "data_length": spec.get("data_length", 8),
            }
            for alias, spec in self._devices.items()
            if isinstance(spec, dict)
        ]

    def count(self) -> int:
        return len(self.list())
