"""A remotely-hostable MCP server for MarkItDown."""

from .__about__ import __version__
from .server import Config, build_server

__all__ = ["Config", "build_server", "__version__"]
