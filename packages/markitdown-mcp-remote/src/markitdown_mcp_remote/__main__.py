"""Command line entry point for the remote MarkItDown MCP server."""

from __future__ import annotations

import argparse
import os

import uvicorn

from .__about__ import __version__
from .server import Config, build_server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="markitdown-mcp-remote",
        description=(
            "Run a remotely-hostable MarkItDown MCP server. Unlike markitdown-mcp, "
            "this one is built to be exposed to the internet: it requires an OAuth "
            "passphrase, refuses file: URIs, and blocks requests to private "
            "addresses. Configure it with environment variables — see the README."
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("HOST", "0.0.0.0"),
        help="Interface to bind (default: 0.0.0.0, since this runs behind a proxy)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8080")),
        help="Port to listen on (default: $PORT, or 8080)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    config = Config.from_env()
    server = build_server(config)

    print(f"MarkItDown MCP (remote) {__version__}")
    print(f"  public URL:   {config.public_url}")
    print(f"  MCP endpoint: {config.public_url}/mcp")
    if config.allowed_hosts:
        print(f"  host allowlist: {', '.join(sorted(config.allowed_hosts))}")
    else:
        print("  host allowlist: (none — any public host may be fetched)")
    print(f"  max document: {config.max_bytes:,} bytes")

    uvicorn.run(
        # host is passed through so the transport's DNS-rebinding protection
        # knows which Host header to consider legitimate.
        server.streamable_http_app(host=args.host),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
