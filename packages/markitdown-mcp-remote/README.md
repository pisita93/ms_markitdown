# MarkItDown MCP (remote)

An MCP server for MarkItDown built to be **hosted on the public internet** and
connected to [claude.ai](https://claude.ai) as a custom connector.

## How this differs from `markitdown-mcp`

The `markitdown-mcp` package in this repository is, by its own README, "meant for
**local use**, with local trusted agents". It binds to localhost, has no
authentication, and accepts `file:` URIs. Those are reasonable choices for a
server talking to an agent on your own machine, and dangerous ones for a server
anyone can reach: without authentication, `file:///etc/passwd` is a valid request,
and `http://169.254.169.254/` reaches your cloud provider's metadata service.

This package exists because making that server safe to expose means changing its
behaviour, not just its bind address:

| | `markitdown-mcp` | `markitdown-mcp-remote` |
| --- | --- | --- |
| Intended network | localhost | public internet |
| Authentication | none | OAuth 2.1, passphrase-gated |
| `file:` URIs | accepted | refused |
| Private/loopback addresses | reachable | blocked, including via redirects |
| Response size | unbounded | capped |
| Host allowlist | — | optional |

Use `markitdown-mcp` for Claude Desktop and Claude Code. Use this one for
claude.ai.

## What it can and cannot convert

**It takes a URL, not a file.** MCP tools receive JSON arguments chosen by the
model — they do not get the attachments in your conversation. A file you upload
to claude.ai never reaches this server, and no MCP server can change that. What
works is giving Claude a link:

> Convert https://arxiv.org/pdf/2308.08155 to markdown and summarise the method.

The URL has to be reachable from the public internet, so a link that requires
your browser session (most SharePoint, Drive, or intranet links) will not work
unless it is a genuine direct-download URL.

Formats are whatever MarkItDown handles: PDF, Word, PowerPoint, Excel, EPUB,
Outlook `.msg`, HTML, CSV, JSON, XML/RSS, Jupyter notebooks and zip archives.
Images and audio need `exiftool` and `ffmpeg`; install them in the container if
you want them (the Dockerfile does).

## Configuration

| Variable | Required | Meaning |
| --- | --- | --- |
| `MARKITDOWN_MCP_PUBLIC_URL` | yes | The `https://` address Claude reaches this server at. OAuth metadata is published under it, so it must match exactly. |
| `MARKITDOWN_MCP_PASSPHRASE` | yes | What you type to authorise a connection. |
| `MARKITDOWN_MCP_ALLOWED_HOSTS` | no | Comma-separated hosts this server may fetch from, subdomains included. Unset means any public host. |
| `MARKITDOWN_MCP_MAX_BYTES` | no | Largest document to download. Default 50 MB. |
| `MARKITDOWN_MCP_TIMEOUT` | no | Per-request timeout in seconds. Default 30. |
| `PORT` | no | Port to listen on. Default 8080. |

Generate a passphrase with:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## Running it

```bash
pip install -e packages/markitdown-mcp-remote

export MARKITDOWN_MCP_PUBLIC_URL=https://mcp.example.com
export MARKITDOWN_MCP_PASSPHRASE=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
markitdown-mcp-remote
```

Or with Docker, from the repository root:

```bash
docker build -f packages/markitdown-mcp-remote/Dockerfile -t markitdown-mcp-remote .
docker run --rm -p 8080:8080 \
  -e MARKITDOWN_MCP_PUBLIC_URL=https://mcp.example.com \
  -e MARKITDOWN_MCP_PASSPHRASE=your-passphrase \
  markitdown-mcp-remote
```

The MCP endpoint is `/mcp`; `/healthz` is an unauthenticated liveness check.

### Hosting

This is a long-running Python process with native dependencies, so it needs a
real container host — Fly.io, Render, Railway, Cloud Run, or a VM. It will *not*
run on Cloudflare Workers or Pages.

Whatever you pick must terminate TLS and give you a stable hostname, which is
what goes in `MARKITDOWN_MCP_PUBLIC_URL`.

## Connecting from claude.ai

1. Deploy the server and confirm `https://your-host/healthz` returns `ok`.
2. In claude.ai, open **Settings → Connectors → Add custom connector**.
3. Enter `https://your-host/mcp`.
4. Claude registers itself and opens a browser window. Enter your passphrase.
5. The `convert_to_markdown` tool becomes available in your conversations.

Remote MCP connectors are a paid-plan feature on claude.ai, and the exact
navigation moves around; if the wording differs from the above, look for custom
or remote MCP connectors in settings.

## Security model

What protects the server:

- **Authentication.** Every `/mcp` request needs a valid OAuth access token.
  Clients may register themselves — that is how Claude connects without you
  pre-provisioning credentials — but registration alone grants nothing. Issuing a
  token requires the passphrase.
- **No local file access.** Only `http` and `https` are accepted.
- **SSRF protection.** Hostnames are resolved and every resulting address is
  checked against loopback, private, link-local, multicast and reserved ranges,
  including IPv4-mapped IPv6 forms. Redirects are followed manually so each hop is
  re-checked, because a public URL may redirect to `127.0.0.1`.
- **Resource limits.** Downloads are streamed and abandoned past the size cap;
  requests time out; redirect chains are bounded.
- **PKCE and redirect validation** are enforced by the MCP SDK's own OAuth
  handlers, not reimplemented here.

What it does *not* protect against, and you should know before deploying:

- **DNS rebinding.** Addresses are validated immediately before connecting, but a
  hostile DNS server could return a public address for the check and a private one
  microseconds later. Setting `MARKITDOWN_MCP_ALLOWED_HOSTS` closes this off, and
  is the right move if you only convert documents from known sites.
- **Anyone with the passphrase has full use of the server.** There is one
  passphrase and no per-user accounts. Treat it as a shared secret and change it
  by restarting with a new value.
- **Denial of service.** There is no rate limiting. Put it behind a proxy or
  platform that provides some if it will be widely reachable.
- **Tokens live in memory.** Restarting drops them and Claude will prompt to
  reconnect. Do not run more than one instance behind a load balancer — a token
  issued by one is not recognised by another.

## Development

```bash
pip install -e packages/markitdown-mcp-remote
python -m markitdown_mcp_remote --host 127.0.0.1 --port 8080
```

With `MARKITDOWN_MCP_PUBLIC_URL=http://localhost:8080`, plain HTTP is permitted so
you can exercise the flow locally; any other host must be `https://`.
