"""Configuration: one parser, read at run time (after load_dotenv), safe defaults."""

from __future__ import annotations

import pytest

from profinet_mcp import cli
from profinet_mcp.config import ConfigError, ProfinetConfig, env_bool

ALL_KEYS = (
    "PROFINET_BACKEND",
    "PROFINET_INTERFACE",
    "PROFINET_CONTROLLER_IP",
    "PROFINET_NETWORK",
    "PROFINET_DCP_PORT",
    "PROFINET_DCP_TIMEOUT",
    "PROFINET_TIMEOUT",
    "PROFINET_MAX_RETRIES",
    "PROFINET_RETRY_BACKOFF_BASE",
    "PROFINET_MOCK_HOST",
    "PROFINET_MOCK_PORT",
    "PROFINET_WRITES_ENABLED",
    "PROFINET_CONFIG_CMDS_ENABLED",
    "DEVICE_MAP_FILE",
    "PROFINET_GSD_PATH",
)


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """No PROFINET settings in os.environ, cwd in an empty directory; undoes whatever load_dotenv adds."""
    for key in ALL_KEYS:
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_defaults_are_safe() -> None:
    config = ProfinetConfig.from_env({})
    assert config.writes_enabled is False  # read-only by default
    assert config.config_cmds_enabled is False
    assert config.backend == "none"  # nothing is contacted until a backend is chosen
    assert (config.mock_host, config.mock_port) == ("127.0.0.1", 5600)


@pytest.mark.parametrize("value", ["1", "true", "True", "t", "yes", "y", "on", " ON "])
def test_true_spellings_agree_everywhere(value: str) -> None:
    # Before the fix the client accepted "t" but not "y" and the tools the reverse.
    assert env_bool("X", False, {"X": value}) is True
    assert ProfinetConfig.from_env({"PROFINET_CONFIG_CMDS_ENABLED": value}).config_cmds_enabled is True


@pytest.mark.parametrize("value", ["0", "false", "f", "no", "n", "off", ""])
def test_false_spellings(value: str) -> None:
    assert env_bool("X", True, {"X": value}) is (value == "")  # empty means "use the default"


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"PROFINET_WRITES_ENABLED": "sure"}, "not a boolean"),
        ({"PROFINET_BACKEND": "tcp"}, "PROFINET_BACKEND"),
        ({"PROFINET_TIMEOUT": "fast"}, "not a valid number"),
        ({"PROFINET_DCP_TIMEOUT": "0.1"}, "between 0.5 and 60"),
        ({"PROFINET_MOCK_PORT": "70000"}, "between 1 and 65535"),
        ({"PROFINET_MAX_RETRIES": "-1"}, "between 0 and 10"),
        ({"PROFINET_CONTROLLER_IP": "plc"}, "IPv4"),
        ({"PROFINET_NETWORK": "10.0.0.0/33"}, "IPv4 network"),
    ],
)
def test_unusable_settings_are_rejected(env: dict, fragment: str) -> None:
    with pytest.raises(ConfigError, match=fragment):
        ProfinetConfig.from_env(env)


def test_cli_reads_every_setting_from_dotenv(isolated_env, monkeypatch) -> None:
    """Regression: ProfinetConfig used to read os.environ at import time, before load_dotenv()."""
    (isolated_env / ".env").write_text(
        "\n".join(
            [
                "PROFINET_BACKEND=mock",
                "PROFINET_INTERFACE=enp3s0",
                "PROFINET_CONTROLLER_IP=10.0.0.1",
                "PROFINET_NETWORK=10.0.0.0/24",
                "PROFINET_DCP_PORT=34965",
                "PROFINET_DCP_TIMEOUT=2.5",
                "PROFINET_TIMEOUT=3",
                "PROFINET_MAX_RETRIES=1",
                "PROFINET_RETRY_BACKOFF_BASE=0.1",
                "PROFINET_MOCK_HOST=127.0.0.2",
                "PROFINET_MOCK_PORT=5601",
                "PROFINET_WRITES_ENABLED=y",
                "PROFINET_CONFIG_CMDS_ENABLED=t",
                "DEVICE_MAP_FILE=devices.json",
                "PROFINET_GSD_PATH=gsd",
            ]
        )
    )
    captured = {}

    def fake_run(self) -> None:
        captured["server"] = self

    monkeypatch.setattr(cli.ProfinetMCPServer, "run", fake_run)
    cli.main([])
    server = captured["server"]
    config = server.config
    assert config.backend == "mock"
    assert config.interface == "enp3s0"
    assert config.controller_ip == "10.0.0.1"
    assert config.network_cidr == "10.0.0.0/24"
    assert config.dcp_port == 34965
    assert config.dcp_timeout == 2.5
    assert config.timeout == 3
    assert config.max_retries == 1
    assert config.retry_backoff_base == 0.1
    assert (config.mock_host, config.mock_port) == ("127.0.0.2", 5601)
    assert config.writes_enabled is True
    assert config.config_cmds_enabled is True
    assert str(config.device_map_path) == "devices.json"
    assert str(config.gsd_base_path) == "gsd"
    # The tools and the client share one config object, so their gates cannot disagree.
    assert server.client.config is config
    assert server.client.backend.port == 5601


def test_cli_env_file_option(isolated_env, monkeypatch) -> None:
    env_file = isolated_env / "custom.env"
    env_file.write_text("PROFINET_BACKEND=network\nPROFINET_INTERFACE=en7\n")
    captured = {}
    monkeypatch.setattr(cli.ProfinetMCPServer, "run", lambda self: captured.setdefault("server", self))
    cli.main(["--env-file", str(env_file)])
    assert captured["server"].config.backend == "network"
    assert captured["server"].config.interface == "en7"


def test_real_environment_wins_over_dotenv(isolated_env, monkeypatch) -> None:
    (isolated_env / ".env").write_text("PROFINET_WRITES_ENABLED=true\n")
    monkeypatch.setenv("PROFINET_WRITES_ENABLED", "false")
    captured = {}
    monkeypatch.setattr(cli.ProfinetMCPServer, "run", lambda self: captured.setdefault("server", self))
    cli.main([])
    assert captured["server"].config.writes_enabled is False
