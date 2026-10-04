"""The profinet-mcp command line: --help/--version must not start the stdio server."""

from __future__ import annotations

import subprocess
import sys

import pytest
from helpers import PROJECT_DIR, clean_env

from profinet_mcp import __version__


def run_cli(*args: str, cwd, env=None, timeout: float = 30) -> subprocess.CompletedProcess:
    # stdin stays open (a pipe nobody writes to): a server that started would block until the timeout.
    return subprocess.run(
        [sys.executable, "-m", "profinet_mcp.cli", *args],
        cwd=cwd,
        env=env or clean_env(),
        stdin=subprocess.PIPE,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_help_prints_usage_and_exits(tmp_path) -> None:
    result = run_cli("--help", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.startswith("usage: profinet-mcp")
    assert "PROFINET_BACKEND" in result.stdout
    assert "PROFINET_WRITES_ENABLED" in result.stdout


def test_version(tmp_path) -> None:
    result = run_cli("--version", cwd=tmp_path)
    assert result.returncode == 0
    assert result.stdout.strip() == f"profinet-mcp {__version__}"


def test_unknown_argument_is_an_error(tmp_path) -> None:
    result = run_cli("--bogus", cwd=tmp_path)
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
    assert result.stdout == ""


def test_bad_configuration_fails_at_startup_on_stderr(tmp_path) -> None:
    result = run_cli(cwd=tmp_path, env=clean_env(PROFINET_WRITES_ENABLED="perhaps"))
    assert result.returncode == 2
    assert "PROFINET_WRITES_ENABLED" in result.stderr
    assert result.stdout == ""  # stdout belongs to JSON-RPC


def test_missing_env_file_is_an_error(tmp_path) -> None:
    result = run_cli("--env-file", str(tmp_path / "nope.env"), cwd=tmp_path)
    assert result.returncode == 2
    assert "does not exist" in result.stderr


@pytest.mark.skipif(
    (PROJECT_DIR / ".env").exists(), reason="a developer's profinet-python/.env would change the server's settings"
)
def test_working_directory_dotenv_is_not_read_in_a_real_launch(tmp_path) -> None:
    """The way an MCP client launches the server: cwd is the client's project, which may hold a .env."""
    (tmp_path / ".env").write_text("PROFINET_BACKEND=mock\nPROFINET_WRITES_ENABLED=true\n")
    result = subprocess.run(
        [sys.executable, "-m", "profinet_mcp.cli"],
        cwd=tmp_path,
        env=clean_env(),
        stdin=subprocess.DEVNULL,  # EOF: the server starts and exits
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert f"not reading {tmp_path / '.env'}" in result.stderr or "not reading" in result.stderr
    assert "backend=none" in result.stderr and "writes disabled" in result.stderr
    assert result.stdout == ""
