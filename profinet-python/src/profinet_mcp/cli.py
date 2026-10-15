"""Command-line entry point: ``profinet-mcp`` serves MCP over stdio."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from . import __version__
from .config import ConfigError, ProfinetConfig
from .server import ProfinetMCPServer

log = logging.getLogger("profinet_mcp")

#: The installed package directory (``.../src/profinet_mcp`` in a source checkout).
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_NAME = "industriagents-profinet-mcp"
#: Variables an implicit .env may set besides PROFINET_* (rule 3b: only the server's own settings).
OWN_EXTRAS = frozenset({"DEVICE_MAP_FILE"})

ENVIRONMENT_HELP = """\
configuration (environment variables, --env-file, or profinet-python/.env):
  PROFINET_BACKEND              none (default) | mock | network
  PROFINET_MOCK_HOST/PORT       mock IO device address (127.0.0.1:5600)
  PROFINET_INTERFACE            Ethernet interface for DCP discovery (eth0)
  PROFINET_WRITES_ENABLED       allow tools that change devices (default false)
  PROFINET_CONFIG_CMDS_ENABLED  also needed for set_device_name, set_device_ip,
                                identify_device (default false)
  DEVICE_MAP_FILE               JSON file of device aliases
  PROFINET_GSD_PATH             directory searched by load_gsd_file
Without --env-file, the only .env read is profinet-python/.env of a source
checkout (never one in the working directory or its parents), and it may set
only PROFINET_* settings and DEVICE_MAP_FILE - not the safety switches
(PROFINET_*_ENABLED), which come from the real environment or --env-file.
See profinet-python/README.md for the full list.

The server speaks MCP on stdin/stdout; start it from an MCP client."""


def project_dir(package_dir: Path | None = None) -> Path | None:
    """``profinet-python/`` of a source checkout, located from the package source; None when installed."""
    package_dir = (package_dir or PACKAGE_DIR).resolve()
    if {"site-packages", "dist-packages"} & set(package_dir.parts):
        return None
    if package_dir.parent.name != "src":
        return None
    project = package_dir.parent.parent
    try:
        metadata = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return project if metadata.get("project", {}).get("name") == PROJECT_NAME else None


def project_dotenv(package_dir: Path | None = None) -> Path | None:
    """The only implicit .env: ``profinet-python/.env`` next to the package source (rule 3c)."""
    project = project_dir(package_dir)
    candidate = project / ".env" if project else None
    return candidate if candidate is not None and candidate.is_file() else None


def env_key(name: str) -> str:
    """Normalise a .env key the way Windows does (upper case), dropping an ``export`` prefix."""
    key = name.strip()
    if key[:7].lower() == "export ":
        key = key[7:].strip()
    return key.upper()


def is_safety_switch(key: str) -> bool:
    return key.startswith("PROFINET_") and key.endswith("_ENABLED")


def is_own_setting(key: str) -> bool:
    return key.startswith("PROFINET_") or key in OWN_EXTRAS


def load_project_dotenv(path: str | os.PathLike[str]) -> tuple[list[str], list[str]]:
    """Apply the implicit .env defensively; return (safety switches ignored, foreign keys ignored).

    Keys are compared case-insensitively (rule 3a). Safety switches are never taken from it (rule 3),
    nor is anything that is not one of the server's own settings (rule 3b). Values already in the
    environment win, as with ``load_dotenv``.
    """
    switches, foreign = [], []
    for name, value in dotenv_values(path).items():
        key = env_key(name)
        if value is None:
            continue
        if is_safety_switch(key):
            switches.append(name)
        elif not is_own_setting(key):
            foreign.append(name)
        elif key not in os.environ:
            os.environ[key] = value
    return switches, foreign


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="profinet-mcp",
        description="PROFINET IO MCP server (stdio transport).",
        epilog=ENVIRONMENT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--env-file",
        type=Path,
        metavar="PATH",
        help=(
            "load settings, safety switches included, from this file (instead of profinet-python/.env, which "
            "may not set safety switches)"
        ),
    )
    return parser


def warn(message: str) -> None:
    print(f"profinet-mcp: warning: {message}", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)  # --help / --version exit here, before anything touches stdout

    if args.env_file is not None:
        if not args.env_file.is_file():
            parser.error(f"--env-file {args.env_file} does not exist")
        load_dotenv(args.env_file)  # explicit: trusted, safety switches included
    else:
        dotenv = project_dotenv()
        if dotenv is not None:
            switches, foreign = load_project_dotenv(dotenv)
            for name in switches:
                warn(
                    f"ignoring {name} from {dotenv}: safety switches are only read from the real environment "
                    "or an explicit --env-file"
                )
            for name in foreign:
                warn(
                    f"ignoring {name} from {dotenv}: an automatically loaded .env may only set PROFINET_* "
                    "settings and DEVICE_MAP_FILE"
                )
        stray = Path.cwd() / ".env"
        if stray.is_file() and (dotenv is None or stray.resolve() != dotenv.resolve()):
            warn(
                f"not reading {stray}: a .env in the working directory is never loaded; pass it with --env-file "
                "if you trust it"
            )

    # Configuration is read only now, after load_dotenv(), so .env settings apply.
    try:
        config = ProfinetConfig.from_env()
    except ConfigError as exc:
        print(f"profinet-mcp: configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

    server = ProfinetMCPServer(config)
    detail = {
        "mock": f" ({config.mock_host}:{config.mock_port})",
        "network": f" (interface {config.interface})",
    }.get(config.backend, " - device tools will report that no backend is selected")
    log.info(
        "PROFINET MCP server %s: backend=%s%s, writes %s, config commands %s",
        __version__,
        config.backend,
        detail,
        "enabled" if config.writes_enabled else "disabled",
        "enabled" if config.config_cmds_enabled else "disabled",
    )
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
