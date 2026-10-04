"""PROFINET MCP server package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("profinet-mcp")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without installing
    __version__ = "0.0.0+unknown"

from .server import ProfinetMCPServer  # noqa: E402  (needs __version__)

__all__ = ["ProfinetMCPServer", "__version__"]
