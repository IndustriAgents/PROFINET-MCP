"""Shared test helpers (imported by the test modules; not a pytest plugin)."""

from __future__ import annotations

import importlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = PROJECT_DIR.parent
MOCK_DIR = REPO_DIR / "profinet-mock-server"
MOCK_SCRIPT = MOCK_DIR / "profinet_mock_device.py"
DATA_DIR = Path(__file__).resolve().parent / "data"
SAMPLE_GSDML = DATA_DIR / "GSDML-V2.35-IndustriAgents-SampleIO-20260101.xml"

#: Settings that must not leak from the developer's shell into subprocess tests.
SCRUBBED_PREFIXES = ("PROFINET_", "MOCK_PROFINET_", "DEVICE_MAP_FILE")


def mock_module() -> Any:
    """Import ../profinet-mock-server/profinet_mock_device.py (its deps are in this dev environment)."""
    if str(MOCK_DIR) not in sys.path:
        sys.path.insert(0, str(MOCK_DIR))
    return importlib.import_module("profinet_mock_device")


def clean_env(**overrides: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED_PREFIXES)}
    env["PYTHONUNBUFFERED"] = "1"
    env.update(overrides)
    return env


def envelope_of(result: Any) -> dict[str, Any]:
    """The envelope from FastMCP.call_tool's (content, structured) result or a CallToolResult."""
    if isinstance(result, tuple):
        return result[1]
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    return json.loads(result.content[0].text)


class MockProcess:
    """profinet-mock-server running as a subprocess on an OS-assigned port."""

    LISTEN_RE = re.compile(r"PROFINET mock listening on (?:\[(?P<v6>[^\]]+)\]|(?P<v4>[^:\s]+)):(?P<port>\d+)")

    def __init__(self, cwd: Path, *extra_args: str) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.output: list[str] = []
        self.proc = subprocess.Popen(
            [sys.executable, str(MOCK_SCRIPT), "--host", "127.0.0.1", "--port", "0", "--update-interval", "3600"]
            + list(extra_args),
            cwd=cwd,
            env=clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        self.port = self._wait_for_port()

    def _drain(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.output.append(line)
            self.lines.put(line)

    def _wait_for_port(self, timeout: float = 20.0) -> int:
        try:
            while True:
                line = self.lines.get(timeout=timeout)
                match = self.LISTEN_RE.search(line)
                if match:
                    return int(match.group("port"))
        except queue.Empty:
            self.stop()
            raise RuntimeError(f"mock did not report its port; output: {''.join(self.output)!r}") from None

    def connections(self) -> int:
        return sum("Client connected" in line for line in self.output)

    def wait_for_connections(self, count: int, timeout: float = 5.0) -> int:
        """Connections seen so far, waiting up to ``timeout`` for the log reader to catch up to ``count``."""
        deadline = time.monotonic() + timeout
        while self.connections() < count and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.connections()

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._reader.join(timeout=5)
