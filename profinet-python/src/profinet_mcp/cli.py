"""Command-line entry point: ``profinet-mcp`` serves MCP over stdio."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path

from dotenv import dotenv_values, find_dotenv, load_dotenv

from . import __version__
from .config import ConfigError, ProfinetConfig
from .server import ProfinetMCPServer

log = logging.getLogger("profinet_mcp")

#: Safety switches. They are honoured only from the real environment or an explicit --env-file, never
#: from a .env that was found automatically: with a direct launch the working directory is the MCP
#: client's project, which may not be trusted.
SAFETY_SWITCH = re.compile(r"^PROFINET_[A-Z0-9_]*_ENABLED$")

ENVIRONMENT_HELP = """\
configuration (environment variables, or a .env file):
  PROFINET_BACKEND              none (default) | mock | network
  PROFINET_MOCK_HOST/PORT       mock IO device address (127.0.0.1:5600)
  PROFINET_INTERFACE            Ethernet interface for DCP discovery (eth0)
  PROFINET_WRITES_ENABLED       allow tools that change devices (default false)
  PROFINET_CONFIG_CMDS_ENABLED  also needed for set_device_name, set_device_ip,
                                identify_device (default false)
  DEVICE_MAP_FILE               JSON file of device aliases
  PROFINET_GSD_PATH             directory searched by load_gsd_file
Safety switches (PROFINET_*_ENABLED) are read only from the real environment or
an explicit --env-file; a .env found in the working directory cannot turn them on.
See profinet-python/README.md for the full list.

The server speaks MCP on stdin/stdout; start it from an MCP client."""


def load_discovered_dotenv(path: str | os.PathLike[str]) -> list[str]:
    """Load an automatically found .env without its safety switches; return the switches it tried to set.

    Like ``load_dotenv``, values already in the environment win.
    """
    ignored = []
    for name, value in dotenv_values(path).items():
        if value is None or name in os.environ:
            continue
        if SAFETY_SWITCH.match(name):
            ignored.append(name)
            continue
        os.environ[name] = value
    return ignored


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
            "load settings, safety switches included, from this file instead of searching the working "
            "directory and its parents for a .env (which may not set safety switches)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)  # --help / --version exit here, before anything touches stdout

    if args.env_file is not None:
        if not args.env_file.is_file():
            parser.error(f"--env-file {args.env_file} does not exist")
        load_dotenv(args.env_file)
    else:
        path = find_dotenv(usecwd=True)
        if path:
            for name in load_discovered_dotenv(path):
                print(
                    f"profinet-mcp: warning: ignoring {name} from {path}: safety switches are only read from the "
                    "real environment or an explicit --env-file",
                    file=sys.stderr,
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
