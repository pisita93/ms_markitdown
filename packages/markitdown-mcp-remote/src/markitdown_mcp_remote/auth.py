"""
A small OAuth authorization server, so the MCP endpoint is not open to the world.

Claude connects to a remote MCP server by registering itself dynamically and
running an OAuth flow, so the server has to *be* an authorization server rather
than simply check a header. This module provides the smallest one that is
honestly secure for a single-operator deployment: clients register themselves,
but issuing a token requires whoever is at the browser to enter a passphrase you
set.

The protocol-critical checks are not implemented here. The MCP SDK's own
handlers verify the PKCE ``code_verifier`` against the stored challenge and
validate redirect URIs against the registered client before this provider is
consulted. What lives here is storage, expiry and the passphrase gate.

State is in memory. Restarting the server invalidates every token, and Claude
will prompt to reconnect — which is fine for one instance and is the reason this
package does not pretend to support horizontal scaling.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

AUTHORIZATION_CODE_TTL = 300  # 5 minutes, per OAuth 2.1 guidance.
ACCESS_TOKEN_TTL = 60 * 60  # 1 hour; Claude refreshes silently.
REFRESH_TOKEN_TTL = 60 * 60 * 24 * 30  # 30 days.

# Pending logins are dropped after this, so an abandoned browser tab does not
# leave an authorization request usable indefinitely.
PENDING_LOGIN_TTL = 600


@dataclass
class _PendingLogin:
    """An authorization request waiting for the passphrase to be entered."""

    client_id: str
    params: AuthorizationParams
    created_at: float = field(default_factory=time.time)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > PENDING_LOGIN_TTL


class PassphraseAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Authorization server whose sole identity check is a shared passphrase."""

    def __init__(self, passphrase: str, login_path: str = "/login") -> None:
        if not passphrase:
            raise ValueError("A passphrase is required; refusing to run without one.")
        self._passphrase = passphrase
        self._login_path = login_path

        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._pending: dict[str, _PendingLogin] = {}
        self._auth_codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}

    # -- client registration ------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Registration is open, which is what lets Claude connect without you
        # pre-provisioning credentials. It grants nothing on its own: a
        # registered client still cannot obtain a token without the passphrase.
        self._clients[client_info.client_id] = client_info

    # -- authorization ------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Send the browser to the passphrase form rather than straight back."""
        self._expire_pending()
        request_id = secrets.token_urlsafe(32)
        self._pending[request_id] = _PendingLogin(client.client_id, params)
        return f"{self._login_path}?request_id={request_id}"

    def pending_login(self, request_id: str) -> _PendingLogin | None:
        pending = self._pending.get(request_id)
        if pending is None:
            return None
        if pending.expired:
            del self._pending[request_id]
            return None
        return pending

    def check_passphrase(self, candidate: str) -> bool:
        # Constant-time, so the comparison does not leak the passphrase one
        # character at a time to someone measuring response times.
        return secrets.compare_digest(candidate, self._passphrase)

    def complete_login(self, request_id: str) -> str:
        """Turn an approved login into a redirect back to the client, with a code.

        Raises ``KeyError`` if the request is unknown or has expired.
        """
        pending = self.pending_login(request_id)
        if pending is None:
            raise KeyError(request_id)
        del self._pending[request_id]

        code = f"mcp_code_{secrets.token_urlsafe(32)}"
        self._auth_codes[code] = AuthorizationCode(
            code=code,
            scopes=pending.params.scopes or [],
            expires_at=time.time() + AUTHORIZATION_CODE_TTL,
            client_id=pending.client_id,
            code_challenge=pending.params.code_challenge,
            redirect_uri=pending.params.redirect_uri,
            redirect_uri_provided_explicitly=pending.params.redirect_uri_provided_explicitly,
            resource=pending.params.resource,
            subject="operator",
        )
        return construct_redirect_uri(
            str(pending.params.redirect_uri),
            code=code,
            state=pending.params.state,
        )

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._auth_codes.get(authorization_code)
        if code is None:
            return None
        if code.client_id != client.client_id or code.expires_at < time.time():
            # A code presented by the wrong client, or too late, is spent.
            self._auth_codes.pop(authorization_code, None)
            return None
        return code

    # -- tokens -------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access = f"mcp_at_{secrets.token_urlsafe(32)}"
        refresh = f"mcp_rt_{secrets.token_urlsafe(32)}"
        now = int(time.time())

        self._access_tokens[access] = AccessToken(
            token=access,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=resource,
            subject="operator",
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh,
            client_id=client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
            subject="operator",
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single use: a code that is replayed must not yield a second token.
        if self._auth_codes.pop(authorization_code.code, None) is None:
            raise ValueError("Authorization code has already been used.")
        return self._issue(
            client.client_id, authorization_code.scopes, authorization_code.resource
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = self._refresh_tokens.get(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        if token.expires_at is not None and token.expires_at < time.time():
            del self._refresh_tokens[refresh_token]
            return None
        return token

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Rotate: the presented refresh token is retired as a new pair is issued,
        # so a stolen one is usable at most once and the theft is detectable.
        self._refresh_tokens.pop(refresh_token.token, None)
        return self._issue(client.client_id, scopes or refresh_token.scopes, None)

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = self._access_tokens.get(token)
        if access is None:
            return None
        if access.expires_at is not None and access.expires_at < time.time():
            del self._access_tokens[token]
            return None
        return access

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)

    async def exchange_identity_assertion(self, client: Any, params: Any) -> OAuthToken:
        raise NotImplementedError(
            "Identity assertion is not supported; this server authenticates with a "
            "passphrase only."
        )

    # -- housekeeping -------------------------------------------------------

    def _expire_pending(self) -> None:
        for request_id in [k for k, v in self._pending.items() if v.expired]:
            del self._pending[request_id]
