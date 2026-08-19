"""
Fetching documents safely on behalf of a remote caller.

A public MCP server that will retrieve any URL it is handed is a
server-side request forgery engine: it sits inside a network the caller
cannot reach, so "fetch this URL and tell me what it says" becomes a way to
read cloud instance metadata, internal admin panels and anything else bound to
a private address.

Everything here exists to keep that from happening:

  * only http and https are accepted — notably not ``file:``, which would hand
    out the contents of the server's own disk;
  * hostnames are resolved and every resulting address is checked against
    private, loopback, link-local and other reserved ranges before connecting;
  * redirects are followed manually so each hop is re-validated, because a
    public URL is free to redirect to ``127.0.0.1``;
  * responses are streamed and abandoned once they exceed a size limit, so a
    caller cannot exhaust memory by pointing the server at a huge file;
  * an optional host allowlist turns the whole thing into a closed system.

Callers pass the returned bytes to MarkItDown's ``convert_stream``. MarkItDown's
own ``convert_uri`` is deliberately not used anywhere in this package: it does
its own fetching and would bypass every check in this module.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse, urlunparse

import requests

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Sent so that sites which vary on the client, or block unknown agents, behave
# predictably. It also identifies the traffic to whoever runs the target site.
USER_AGENT = "markitdown-mcp-remote (+https://github.com/microsoft/markitdown)"


class FetchError(Exception):
    """A URL could not be fetched, for a reason worth telling the caller."""


@dataclass(frozen=True)
class FetchedDocument:
    content: bytes
    url: str
    content_type: str | None

    @property
    def mimetype(self) -> str | None:
        """The media type alone, with any charset or boundary removed."""
        if not self.content_type:
            return None
        return self.content_type.split(";")[0].strip() or None

    @property
    def charset(self) -> str | None:
        for part in (self.content_type or "").split(";")[1:]:
            key, _, value = part.partition("=")
            if key.strip().lower() == "charset":
                return value.strip().strip('"') or None
        return None


def _addresses_for(hostname: str) -> list[ipaddress._BaseAddress]:
    """Every address a hostname resolves to, v4 and v6."""
    try:
        results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve host {hostname!r}: {exc}") from exc

    addresses = []
    for family, _type, _proto, _canonname, sockaddr in results:
        if family in (socket.AF_INET, socket.AF_INET6):
            try:
                addresses.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                continue
    if not addresses:
        raise FetchError(f"Host {hostname!r} resolved to no usable address.")
    return addresses


def _reject_if_not_public(address: ipaddress._BaseAddress) -> None:
    """Raise unless the address is one a caller could have reached themselves."""
    # is_global is the broadest check and covers most of what follows, but the
    # individual properties are listed so the failure message can say which rule
    # tripped, and so behaviour does not silently change with the stdlib.
    for label, disallowed in (
        ("loopback", address.is_loopback),
        ("private", address.is_private),
        ("link-local", address.is_link_local),
        ("multicast", address.is_multicast),
        ("reserved", address.is_reserved),
        ("unspecified", address.is_unspecified),
        ("non-global", not address.is_global),
    ):
        if disallowed:
            raise FetchError(
                f"Refusing to fetch {address}: it is a {label} address. This "
                "server only retrieves publicly routable URLs."
            )

    # An IPv6 address can wrap a IPv4 one (::ffff:127.0.0.1, 64:ff9b::/96), which
    # the checks above do not see through.
    mapped = getattr(address, "ipv4_mapped", None) or getattr(address, "sixtofour", None)
    if mapped is not None:
        _reject_if_not_public(mapped)


def _check_host_allowed(hostname: str, allowed_hosts: frozenset[str] | None) -> None:
    if not allowed_hosts:
        return
    candidate = hostname.lower().rstrip(".")
    for allowed in allowed_hosts:
        if candidate == allowed or candidate.endswith("." + allowed):
            return
    raise FetchError(
        f"Refusing to fetch from {hostname!r}: this server is configured with an "
        "allowlist and that host is not on it."
    )


def validate_url(url: str, allowed_hosts: frozenset[str] | None = None) -> str:
    """Check a URL is one this server is willing to retrieve.

    Returns the normalised URL. Raises :class:`FetchError` otherwise.
    """
    parsed = urlparse(url)

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise FetchError(
            f"Unsupported URL scheme {parsed.scheme or '(none)'!r}. This server "
            "accepts http and https only — it cannot read local files."
        )

    hostname = parsed.hostname
    if not hostname:
        raise FetchError(f"No host found in URL {url!r}.")

    _check_host_allowed(hostname, allowed_hosts)
    for address in _addresses_for(hostname):
        _reject_if_not_public(address)

    return urlunparse(parsed)


def _read_capped(response: requests.Response, max_bytes: int) -> bytes:
    """Read a response body, refusing to buffer more than ``max_bytes``."""
    # Trust the declared length when it is already too large, so an oversized
    # download is refused before any of it is transferred.
    declared = response.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise FetchError(
            f"Document is {int(declared):,} bytes, over this server's "
            f"{max_bytes:,} byte limit."
        )

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(
                f"Document exceeds this server's {max_bytes:,} byte limit."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_document(
    url: str,
    *,
    max_bytes: int,
    timeout: float,
    allowed_hosts: frozenset[str] | None = None,
    max_redirects: int = 5,
) -> FetchedDocument:
    """Retrieve a document, validating the destination at every redirect hop.

    Redirects are followed by hand rather than by requests, because a URL that
    validates can still redirect somewhere that would not.
    """
    current = url
    with requests.Session() as session:
        session.max_redirects = 0
        for _hop in range(max_redirects + 1):
            current = validate_url(current, allowed_hosts)
            try:
                response = session.get(
                    current,
                    stream=True,
                    timeout=timeout,
                    allow_redirects=False,
                    headers={"User-Agent": USER_AGENT},
                )
            except requests.RequestException as exc:
                raise FetchError(f"Could not fetch {current}: {exc}") from exc

            with response:
                if response.is_redirect or response.is_permanent_redirect:
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchError(
                            f"{current} returned a redirect with no destination."
                        )
                    current = requests.compat.urljoin(current, location)
                    continue

                if response.status_code >= 400:
                    raise FetchError(
                        f"{current} returned HTTP {response.status_code}."
                    )

                return FetchedDocument(
                    content=_read_capped(response, max_bytes),
                    url=current,
                    content_type=response.headers.get("Content-Type"),
                )

    raise FetchError(f"Gave up after {max_redirects} redirects starting at {url}.")


def parse_allowed_hosts(raw: str | Iterable[str] | None) -> frozenset[str] | None:
    """Build a host allowlist from configuration. ``None`` means no allowlist."""
    if raw is None:
        return None
    items = raw.split(",") if isinstance(raw, str) else list(raw)
    hosts = {h.strip().lower().rstrip(".") for h in items}
    hosts.discard("")
    return frozenset(hosts) or None
