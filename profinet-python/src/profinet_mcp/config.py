"""Server configuration, read from the environment when the server starts.

Nothing here runs at import time: :meth:`ProfinetConfig.from_env` is called by
the CLI after ``load_dotenv()``, so settings in a ``.env`` file are honoured.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "off"})

BACKEND_NONE = "none"
BACKEND_MOCK = "mock"
BACKEND_NETWORK = "network"
BACKENDS = (BACKEND_NONE, BACKEND_MOCK, BACKEND_NETWORK)


class ConfigError(ValueError):
    """An environment setting that cannot be used. The server refuses to start."""


def env_bool(name: str, default: bool, environ: Mapping[str, str] | None = None) -> bool:
    """Parse a boolean setting. Unset or empty means ``default``; junk is an error."""
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ConfigError(f"{name}={raw!r} is not a boolean; use true or false (1/0, yes/no and on/off also work)")


def _env_str(env: Mapping[str, str], name: str, default: str) -> str:
    raw = env.get(name)
    return raw.strip() if raw is not None and raw.strip() else default


def _env_number(
    env: Mapping[str, str], name: str, default: float, kind: type, low: float, high: float | None = None
) -> float:
    raw = env.get(name)
    if raw is None or not raw.strip():
        return kind(default)
    try:
        value = kind(raw.strip())
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a valid {'integer' if kind is int else 'number'}") from None
    if value < low or (high is not None and value > high):
        bound = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ConfigError(f"{name}={raw!r} must be {bound}")
    return value


def _env_path(env: Mapping[str, str], name: str) -> Path | None:
    raw = env.get(name)
    return Path(raw.strip()).expanduser() if raw and raw.strip() else None


@dataclass(slots=True, frozen=True)
class ProfinetConfig:
    """Every setting the server uses. Safe defaults: no backend, no writes."""

    backend: str = BACKEND_NONE
    interface: str = "eth0"
    controller_ip: str = "192.168.1.1"
    network_cidr: str = "192.168.1.0/24"
    dcp_port: int = 34964
    dcp_timeout: float = 5.0
    timeout: float = 5.0
    max_retries: int = 3
    retry_backoff_base: float = 0.5
    mock_host: str = "127.0.0.1"
    mock_port: int = 5600
    writes_enabled: bool = False
    config_cmds_enabled: bool = False
    device_map_path: Path | None = None
    gsd_base_path: Path | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> ProfinetConfig:
        """Build the configuration from ``environ`` (default: ``os.environ``)."""
        env = os.environ if environ is None else environ

        backend = _env_str(env, "PROFINET_BACKEND", BACKEND_NONE).lower()
        if backend not in BACKENDS:
            raise ConfigError(f"PROFINET_BACKEND={backend!r} is not one of: {', '.join(BACKENDS)}")

        controller_ip = _env_str(env, "PROFINET_CONTROLLER_IP", "192.168.1.1")
        try:
            ipaddress.IPv4Address(controller_ip)
        except ValueError:
            raise ConfigError(f"PROFINET_CONTROLLER_IP={controller_ip!r} is not an IPv4 address") from None
        network = _env_str(env, "PROFINET_NETWORK", "192.168.1.0/24")
        try:
            ipaddress.IPv4Network(network, strict=False)
        except ValueError:
            raise ConfigError(f"PROFINET_NETWORK={network!r} is not an IPv4 network such as 192.168.1.0/24") from None

        return cls(
            backend=backend,
            interface=_env_str(env, "PROFINET_INTERFACE", "eth0"),
            controller_ip=controller_ip,
            network_cidr=network,
            dcp_port=int(_env_number(env, "PROFINET_DCP_PORT", 34964, int, 1, 65535)),
            dcp_timeout=float(_env_number(env, "PROFINET_DCP_TIMEOUT", 5.0, float, 0.5, 60)),
            timeout=float(_env_number(env, "PROFINET_TIMEOUT", 5.0, float, 0.1, 600)),
            max_retries=int(_env_number(env, "PROFINET_MAX_RETRIES", 3, int, 0, 10)),
            retry_backoff_base=float(_env_number(env, "PROFINET_RETRY_BACKOFF_BASE", 0.5, float, 0, 30)),
            mock_host=_env_str(env, "PROFINET_MOCK_HOST", "127.0.0.1"),
            mock_port=int(_env_number(env, "PROFINET_MOCK_PORT", 5600, int, 1, 65535)),
            writes_enabled=env_bool("PROFINET_WRITES_ENABLED", False, env),
            config_cmds_enabled=env_bool("PROFINET_CONFIG_CMDS_ENABLED", False, env),
            device_map_path=_env_path(env, "DEVICE_MAP_FILE"),
            gsd_base_path=_env_path(env, "PROFINET_GSD_PATH"),
        )
