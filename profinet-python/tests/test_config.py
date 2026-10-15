"""Configuration: one parser, read at run time (after load_dotenv), safe defaults."""

from __future__ import annotations

import os
from types import SimpleNamespace

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


FOREIGN_KEYS = ("PATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "SCAPY_USE_LIBPCAP", "FASTMCP_LOG_LEVEL")


@pytest.fixture
def isolated_env(monkeypatch, tmp_path):
    """No PROFINET settings in os.environ, cwd in an empty directory, and a fake source checkout.

    ``isolated_env.project`` is a throwaway ``profinet-python/`` whose package directory the CLI is
    pointed at, so tests can write its .env without touching the real repository. ``.cwd`` is the
    working directory (outside the project). Whatever the CLI adds to os.environ is undone.
    """
    for key in (*ALL_KEYS, *(k.lower() for k in ALL_KEYS), "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "SCAPY_USE_LIBPCAP"):
        monkeypatch.setenv(key, "placeholder")
        monkeypatch.delenv(key)
    for key in ("PATH", "PYTHONPATH", "FASTMCP_LOG_LEVEL"):
        monkeypatch.setenv(key, os.environ.get(key, ""))  # restored afterwards whatever happens
    project = tmp_path / "checkout" / "profinet-python"
    package = project / "src" / "profinet_mcp"
    package.mkdir(parents=True)
    (project / "pyproject.toml").write_text('[project]\nname = "industriagents-profinet-mcp"\n')
    monkeypatch.setattr(cli, "PACKAGE_DIR", package)
    cwd = tmp_path / "client-project" / "sub"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    return SimpleNamespace(project=project, cwd=cwd, dotenv=project / ".env")


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


def test_cli_reads_every_setting_from_dotenv(isolated_env, monkeypatch, capsys) -> None:
    """Regression: ProfinetConfig used to read os.environ at import time, before load_dotenv().

    profinet-python/.env sets connection settings, but its safety switches are ignored with a warning.
    """
    isolated_env.dotenv.write_text(
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
    # Safety switches from an automatically found .env are ignored, loudly.
    assert config.writes_enabled is False
    assert config.config_cmds_enabled is False
    stderr = capsys.readouterr().err
    assert "ignoring PROFINET_WRITES_ENABLED" in stderr
    assert "ignoring PROFINET_CONFIG_CMDS_ENABLED" in stderr
    assert str(config.device_map_path) == "devices.json"
    assert str(config.gsd_base_path) == "gsd"
    # The tools and the client share one config object, so their gates cannot disagree.
    assert server.client.config is config
    assert server.client.backend.port == 5601


def run_cli(monkeypatch, argv: list[str]):
    captured = {}
    monkeypatch.setattr(cli.ProfinetMCPServer, "run", lambda self: captured.setdefault("server", self))
    cli.main(argv)
    return captured["server"].config


def test_cli_env_file_option(isolated_env, monkeypatch, capsys) -> None:
    """An explicit --env-file is trusted, safety switches included."""
    env_file = isolated_env.cwd / "custom.env"
    env_file.write_text(
        "PROFINET_BACKEND=network\nPROFINET_INTERFACE=en7\n"
        "PROFINET_WRITES_ENABLED=true\nPROFINET_CONFIG_CMDS_ENABLED=true\n"
    )
    config = run_cli(monkeypatch, ["--env-file", str(env_file)])
    assert config.backend == "network"
    assert config.interface == "en7"
    assert config.writes_enabled is True and config.config_cmds_enabled is True
    assert "ignoring" not in capsys.readouterr().err


# -- rule 3c: the implicit .env is only profinet-python/.env ------------------------------------


@pytest.mark.parametrize("where", ["cwd", "parent", "grandparent"])
def test_dotenv_in_working_directory_or_parents_is_not_read(isolated_env, monkeypatch, capsys, where: str) -> None:
    target = {"cwd": isolated_env.cwd, "parent": isolated_env.cwd.parent, "grandparent": isolated_env.cwd.parent.parent}
    (target[where] / ".env").write_text("PROFINET_BACKEND=mock\nPROFINET_MOCK_PORT=5999\nPROFINET_WRITES_ENABLED=1\n")
    config = run_cli(monkeypatch, [])
    assert (config.backend, config.mock_port, config.writes_enabled) == ("none", 5600, False)
    if where == "cwd":
        assert "not reading" in capsys.readouterr().err


def test_project_dotenv_is_found_from_the_package_source(isolated_env) -> None:
    assert cli.project_dir() == isolated_env.project.resolve()
    assert cli.project_dotenv() is None
    isolated_env.dotenv.write_text("PROFINET_BACKEND=mock\n")
    assert cli.project_dotenv() == isolated_env.dotenv.resolve()


@pytest.mark.parametrize("layout", ["flat", "src-like"])
def test_installed_package_has_no_implicit_dotenv(tmp_path, layout: str) -> None:
    """Under site-packages there is never an implicit .env, even if the files around it look like a checkout."""
    site = tmp_path / "venv" / "lib" / "python3.11" / "site-packages"
    package = site / "profinet_mcp" if layout == "flat" else site / "checkout" / "src" / "profinet_mcp"
    package.mkdir(parents=True)
    project = package.parent if layout == "flat" else package.parent.parent
    (project / ".env").write_text("PROFINET_BACKEND=mock\n")
    (project / "pyproject.toml").write_text('[project]\nname = "industriagents-profinet-mcp"\n')
    assert cli.project_dir(package) is None and cli.project_dotenv(package) is None


def test_other_projects_are_not_mistaken_for_ours(tmp_path) -> None:
    package = tmp_path / "other" / "src" / "profinet_mcp"
    package.mkdir(parents=True)
    (package.parent.parent / "pyproject.toml").write_text('[project]\nname = "something-else"\n')
    assert cli.project_dir(package) is None


def test_this_checkout_is_detected() -> None:
    """In this repository (editable install) the project directory is profinet-python/ itself."""
    from helpers import PROJECT_DIR

    assert cli.project_dir() == PROJECT_DIR.resolve()


# -- rules 3, 3a, 3b: what the implicit .env may set ------------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "PROFINET_WRITES_ENABLED=true",
        "profinet_writes_enabled=true",
        "Profinet_Config_Cmds_Enabled=true",
        "export PROFINET_WRITES_ENABLED=true",
        'PROFINET_WRITES_ENABLED="true"',
        "PROFINET_CONFIG_CMDS_ENABLED='1'",
        "  profinet_future_enabled = yes",
    ],
)
def test_project_dotenv_cannot_turn_on_a_safety_switch(isolated_env, monkeypatch, capsys, line: str) -> None:
    isolated_env.dotenv.write_text(f"PROFINET_BACKEND=mock\n{line}\n")
    config = run_cli(monkeypatch, [])
    assert config.backend == "mock"  # its own non-gate settings still apply
    assert config.writes_enabled is False and config.config_cmds_enabled is False
    assert not any(cli.is_safety_switch(cli.env_key(key)) for key in os.environ)
    assert "ignoring" in capsys.readouterr().err


class UpperCasingEnviron(dict):
    """os.environ as on Windows: keys are case-insensitive and stored upper-cased."""

    def __init__(self, data) -> None:
        super().__init__({key.upper(): value for key, value in data.items()})

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key.upper(), value)

    def __getitem__(self, key):
        return super().__getitem__(key.upper())

    def __contains__(self, key) -> bool:
        return super().__contains__(key.upper())

    def get(self, key, default=None):
        return super().get(key.upper(), default)


@pytest.mark.parametrize("line", ["profinet_writes_enabled=true", "Profinet_Config_Cmds_Enabled=true"])
def test_switches_stay_off_where_keys_are_upper_cased(isolated_env, monkeypatch, capsys, line: str) -> None:
    """Rule 3a: on Windows profinet_writes_enabled=true would become PROFINET_WRITES_ENABLED."""
    monkeypatch.setattr(os, "environ", UpperCasingEnviron(os.environ))
    isolated_env.dotenv.write_text(f"PROFINET_BACKEND=mock\n{line}\n")
    config = run_cli(monkeypatch, [])
    assert config.writes_enabled is False and config.config_cmds_enabled is False
    assert f"ignoring {line.split('=')[0]}" in capsys.readouterr().err


@pytest.mark.parametrize("key", FOREIGN_KEYS)
def test_project_dotenv_only_sets_the_servers_own_variables(isolated_env, monkeypatch, capsys, key: str) -> None:
    """Rule 3b: no PATH, LD_PRELOAD, DYLD_*, PYTHON*, SCAPY_* ... from an implicit .env."""
    before = os.environ.get(key)
    isolated_env.dotenv.write_text(f"PROFINET_BACKEND=mock\n{key}=/tmp/injected\n{key.lower()}=/tmp/injected\n")
    config = run_cli(monkeypatch, [])
    assert config.backend == "mock"
    assert os.environ.get(key) == before
    assert f"ignoring {key} from" in capsys.readouterr().err


def test_own_keys_are_normalised(isolated_env, monkeypatch) -> None:
    isolated_env.dotenv.write_text("profinet_backend=mock\nexport Profinet_Mock_Port=5611\ndevice_map_file=map.json\n")
    config = run_cli(monkeypatch, [])
    assert (config.backend, config.mock_port, str(config.device_map_path)) == ("mock", 5611, "map.json")


def test_real_environment_can_enable_switches(isolated_env, monkeypatch, capsys) -> None:
    isolated_env.dotenv.write_text("PROFINET_BACKEND=mock\n")
    monkeypatch.setenv("PROFINET_WRITES_ENABLED", "true")
    monkeypatch.setenv("PROFINET_CONFIG_CMDS_ENABLED", "true")
    config = run_cli(monkeypatch, [])
    assert config.writes_enabled is True and config.config_cmds_enabled is True
    assert "ignoring" not in capsys.readouterr().err


def test_real_environment_wins_over_dotenv(isolated_env, monkeypatch) -> None:
    isolated_env.dotenv.write_text("PROFINET_MOCK_PORT=5999\n")
    monkeypatch.setenv("PROFINET_MOCK_PORT", "5700")
    assert run_cli(monkeypatch, []).mock_port == 5700
