"""
The MCP server: one tool, wrapped in the protections this package exists to add.

The tool deliberately takes a URL rather than file content. An MCP tool receives
JSON arguments chosen by the model, not the attachments in a conversation, so
"convert the file I uploaded" is not something a remote server can do — the
bytes never reach it. Handing it a link is the flow that works.
"""

from __future__ import annotations

import html
import io
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from markitdown import MarkItDown, StreamInfo
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)

from .auth import PassphraseAuthProvider
from .fetching import FetchError, fetch_document, parse_allowed_hosts

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Config:
    public_url: str
    passphrase: str
    allowed_hosts: frozenset[str] | None = None
    max_bytes: int = DEFAULT_MAX_BYTES
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "Config":
        public_url = os.environ.get("MARKITDOWN_MCP_PUBLIC_URL", "").strip()
        passphrase = os.environ.get("MARKITDOWN_MCP_PASSPHRASE", "")

        missing = [
            name
            for name, value in (
                ("MARKITDOWN_MCP_PUBLIC_URL", public_url),
                ("MARKITDOWN_MCP_PASSPHRASE", passphrase),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + "\n\nMARKITDOWN_MCP_PUBLIC_URL is the https:// address Claude will "
                "reach this server at; it must match exactly, because OAuth "
                "metadata is published under it.\nMARKITDOWN_MCP_PASSPHRASE is what "
                "you type to authorise a connection. Generate one with:\n"
                "    python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
            )

        if not public_url.startswith("https://") and "localhost" not in public_url:
            raise SystemExit(
                f"MARKITDOWN_MCP_PUBLIC_URL is {public_url!r}. It must be https:// — "
                "OAuth tokens would otherwise cross the network in the clear."
            )

        return cls(
            public_url=public_url.rstrip("/"),
            passphrase=passphrase,
            allowed_hosts=parse_allowed_hosts(
                os.environ.get("MARKITDOWN_MCP_ALLOWED_HOSTS") or None
            ),
            max_bytes=int(os.environ.get("MARKITDOWN_MCP_MAX_BYTES", DEFAULT_MAX_BYTES)),
            timeout=float(os.environ.get("MARKITDOWN_MCP_TIMEOUT", DEFAULT_TIMEOUT)),
        )


def _extension_from_url(url: str) -> str | None:
    path = urlparse(url).path
    _, dot, suffix = path.rpartition(".")
    if dot and suffix and "/" not in suffix and len(suffix) <= 12:
        return "." + suffix.lower()
    return None


def build_server(config: Config) -> MCPServer:
    provider = PassphraseAuthProvider(config.passphrase)

    server = MCPServer(
        name="markitdown",
        instructions=(
            "Converts documents at public http(s) URLs into Markdown, using "
            "Microsoft MarkItDown. Handles PDF, Word, PowerPoint, Excel, EPUB, "
            "Outlook messages, HTML, CSV, JSON and notebooks. Pass a direct link "
            "to a file; the server fetches and converts it. It cannot read files "
            "attached to the conversation, nor anything on a private network."
        ),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(config.public_url),
            resource_server_url=AnyHttpUrl(config.public_url),
            client_registration_options=ClientRegistrationOptions(enabled=True),
        ),
    )

    @server.tool()
    def convert_to_markdown(url: str) -> str:
        """Fetch a document from a public http(s) URL and convert it to Markdown.

        Use this for links to PDF, Word, PowerPoint, Excel, EPUB, Outlook .msg,
        HTML, CSV, JSON or Jupyter notebook files. The URL must be reachable from
        the public internet and must point directly at the file.

        Args:
            url: A direct http:// or https:// link to the document.
        """
        try:
            document = fetch_document(
                url,
                max_bytes=config.max_bytes,
                timeout=config.timeout,
                allowed_hosts=config.allowed_hosts,
            )
        except FetchError as exc:
            # These messages explain a refusal the model should not retry
            # blindly, so they are surfaced rather than swallowed.
            raise ValueError(str(exc)) from exc

        # convert_stream, never convert_uri: MarkItDown would otherwise do its
        # own fetching and bypass every check in fetching.py.
        result = MarkItDown(enable_plugins=False).convert_stream(
            io.BytesIO(document.content),
            stream_info=StreamInfo(
                mimetype=document.mimetype,
                charset=document.charset,
                extension=_extension_from_url(document.url),
                url=document.url,
            ),
        )

        markdown = result.markdown or ""
        if not markdown.strip():
            raise ValueError(
                f"MarkItDown produced no text for {document.url}. Images and audio "
                "need external tools (exiftool, ffmpeg) that this server does not "
                "run, and scanned PDFs need OCR."
            )
        return markdown

    _add_login_routes(server, provider)
    return server


_LOGIN_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect to MarkItDown</title>
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; background:#f2efe6;
         color:#17150f; display:grid; place-items:center; min-height:100vh; margin:0; }}
  form {{ background:#f8f6f0; border:1px solid #d3ccb9; border-radius:4px;
          padding:2rem; width:min(24rem, 90vw); }}
  h1 {{ font-size:1.25rem; margin:0 0 .35rem; }}
  p {{ color:#565344; font-size:.9rem; margin:0 0 1.25rem; line-height:1.5; }}
  label {{ display:block; font-size:.8rem; margin-bottom:.35rem; }}
  input {{ width:100%; padding:.55rem .6rem; font:inherit; border:1px solid #d3ccb9;
           border-radius:3px; background:#fff; box-sizing:border-box; }}
  button {{ margin-top:1rem; width:100%; padding:.6rem; font:inherit; cursor:pointer;
            background:#17150f; color:#f2efe6; border:0; border-radius:3px; }}
  .error {{ color:#c0341a; font-size:.85rem; margin:.75rem 0 0; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background:#14130f; color:#ece7d8; }}
    form {{ background:#1e1c17; border-color:#34322a; }}
    p {{ color:#a7a191; }}
    input {{ background:#14130f; color:#ece7d8; border-color:#34322a; }}
    button {{ background:#ece7d8; color:#14130f; }}
  }}
</style>
<form method="post" action="/login">
  <h1>Connect to MarkItDown</h1>
  <p>Enter the passphrase for this server to let Claude convert documents through it.</p>
  <input type="hidden" name="request_id" value="{request_id}">
  <label for="passphrase">Passphrase</label>
  <input id="passphrase" name="passphrase" type="password" autocomplete="current-password"
         autofocus required>
  {error}
  <button type="submit">Authorise</button>
</form>
"""

_EXPIRED_PAGE = """<!doctype html>
<meta charset="utf-8"><title>Request expired</title>
<body style="font-family:ui-sans-serif,system-ui,sans-serif;padding:2rem;max-width:34rem">
<h1 style="font-size:1.2rem">This sign-in request is no longer valid</h1>
<p style="color:#565344;line-height:1.5">It may have expired, or already been used.
Start the connection again from Claude.</p>
"""


def _login_page(request_id: str, error: str | None = None) -> HTMLResponse:
    return HTMLResponse(
        _LOGIN_PAGE.format(
            request_id=html.escape(request_id, quote=True),
            error=f'<p class="error">{html.escape(error)}</p>' if error else "",
        ),
        # A wrong passphrase should not be cached or replayed from history.
        headers={"Cache-Control": "no-store"},
        status_code=200 if error is None else 401,
    )


def _add_login_routes(server: MCPServer, provider: PassphraseAuthProvider) -> None:
    """Attach the passphrase form the authorize step redirects to.

    These routes are intentionally unauthenticated — they are how a caller
    authenticates in the first place. The passphrase check is the gate.
    """

    @server.custom_route("/login", methods=["GET"])
    async def get_login(request: Request) -> HTMLResponse:
        request_id = request.query_params.get("request_id", "")
        if provider.pending_login(request_id) is None:
            return HTMLResponse(_EXPIRED_PAGE, status_code=400)
        return _login_page(request_id)

    @server.custom_route("/login", methods=["POST"])
    async def post_login(request: Request):
        form = await request.form()
        request_id = str(form.get("request_id", ""))
        passphrase = str(form.get("passphrase", ""))

        if provider.pending_login(request_id) is None:
            return HTMLResponse(_EXPIRED_PAGE, status_code=400)

        if not provider.check_passphrase(passphrase):
            return _login_page(request_id, "That passphrase is not correct.")

        try:
            redirect_to = provider.complete_login(request_id)
        except KeyError:
            return HTMLResponse(_EXPIRED_PAGE, status_code=400)

        # 302 so the browser re-issues the follow-up as a GET.
        return RedirectResponse(redirect_to, status_code=302)

    # A liveness endpoint, so a host platform can tell the process is up
    # without needing credentials.
    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request):
        return PlainTextResponse("ok")
