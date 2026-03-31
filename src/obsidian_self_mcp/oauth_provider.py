"""OIDC-delegating OAuth authorization server provider for MCP.

Implements the MCP SDK's OAuthAuthorizationServerProvider interface.
Delegates user authentication to any standards-compliant OIDC provider
while acting as the OAuth authorization server for MCP clients.
"""

import logging
import secrets
import time
from urllib.parse import urlencode

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from .config import Config
from .oauth_store import OAuthStore

logger = logging.getLogger(__name__)

# Token lifetimes
AUTH_CODE_TTL = 600  # 10 minutes
ACCESS_TOKEN_TTL = 3600  # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days


class EphemeralStore:
    """In-memory store for short-lived auth codes and state mappings.

    Entries self-expire and are swept on save() to prevent unbounded growth.
    """

    def __init__(self):
        self._store: dict[str, dict] = {}

    def save(self, key: str, value: dict) -> None:
        self._sweep_expired()
        self._store[key] = value

    def get(self, key: str) -> dict | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.get("expires_at", float("inf")) < time.time():
            del self._store[key]
            return None
        return entry

    def pop(self, key: str) -> dict | None:
        entry = self.get(key)
        if entry is not None:
            self._store.pop(key, None)
        return entry

    def _sweep_expired(self) -> None:
        now = time.time()
        expired = [k for k, v in self._store.items() if v.get("expires_at", float("inf")) < now]
        for k in expired:
            del self._store[k]


class OIDCDelegatingProvider(OAuthAuthorizationServerProvider):
    """OAuth AS that delegates authentication to an upstream OIDC provider."""

    def __init__(
        self,
        config: Config,
        store: OAuthStore,
        http_client: httpx.AsyncClient,
        resource_url: str,
        api_key: str | None = None,
    ):
        self.config = config
        self.store = store
        self.http_client = http_client
        self.resource_url = resource_url
        self.api_key = api_key
        self.ephemeral = EphemeralStore()

        # Populated by initialize()
        self._authorization_endpoint: str = ""
        self._token_endpoint: str = ""
        self._jwks_uri: str = ""
        self._jwks: dict = {}

    async def initialize(self) -> None:
        """Fetch OIDC discovery document and JWKS. Must be called before use."""
        discovery_url = f"{self.config.oauth_issuer_url}/.well-known/openid-configuration"
        resp = await self.http_client.get(discovery_url)
        if resp.status_code != 200:
            raise RuntimeError(f"OIDC discovery failed at {discovery_url}: HTTP {resp.status_code}")
        discovery = resp.json()

        self._authorization_endpoint = discovery["authorization_endpoint"]
        self._token_endpoint = discovery["token_endpoint"]
        self._jwks_uri = discovery["jwks_uri"]

        await self._refresh_jwks()
        logger.info("OIDC provider initialized from %s", self.config.oauth_issuer_url)

    async def _refresh_jwks(self) -> None:
        resp = await self.http_client.get(self._jwks_uri)
        if resp.status_code != 200:
            raise RuntimeError(f"JWKS fetch failed at {self._jwks_uri}: HTTP {resp.status_code}")
        self._jwks = resp.json()

    @property
    def jwks_algorithms(self) -> list[str]:
        return [key["alg"] for key in self._jwks.get("keys", []) if "alg" in key]

    # ── OAuthAuthorizationServerProvider interface ─────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await self.store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self.store.save_client(client_info)
        logger.info("Registered OAuth client: %s", client_info.client_id)

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Store client's auth params and redirect to OIDC provider for login."""
        internal_state = secrets.token_urlsafe(32)

        self.ephemeral.save(
            f"state:{internal_state}",
            {
                "original_state": params.state,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "scopes": params.scopes or [],
                "resource": str(params.resource) if params.resource else None,
                "client_id": client.client_id,
                "expires_at": time.time() + AUTH_CODE_TTL,
            },
        )

        # Build OIDC authorization URL
        oidc_params = {
            "response_type": "code",
            "client_id": self.config.oauth_client_id,
            "redirect_uri": self._callback_url,
            "state": internal_state,
            "scope": "openid email profile",
        }
        redirect_url = f"{self._authorization_endpoint}?{urlencode(oidc_params)}"
        return redirect_url

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        entry = self.ephemeral.get(f"code:{authorization_code}")
        if entry is None:
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=entry["scopes"],
            expires_at=entry["expires_at"],
            client_id=entry["client_id"],
            code_challenge=entry["code_challenge"],
            redirect_uri=entry["redirect_uri"],
            redirect_uri_provided_explicitly=entry["redirect_uri_provided_explicitly"],
            resource=entry.get("resource"),
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Delete the auth code (one-time use)
        self.ephemeral.pop(f"code:{authorization_code.code}")

        now = int(time.time())
        token_pair_id = secrets.token_urlsafe(16)
        access_token_str = secrets.token_urlsafe(32)
        refresh_token_str = secrets.token_urlsafe(32)

        access_token = AccessToken(
            token=access_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=authorization_code.resource,
        )
        refresh_token = RefreshToken(
            token=refresh_token_str,
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
        )

        try:
            await self.store.save_access_token(access_token, token_pair_id)
            await self.store.save_refresh_token(refresh_token, token_pair_id)
        except Exception as e:
            logger.exception("Failed to save tokens during authorization code exchange")
            raise TokenError(error="server_error", error_description="internal error") from e

        return OAuthToken(
            access_token=access_token_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token_str,
            scope=" ".join(authorization_code.scopes) if authorization_code.scopes else None,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        return await self.store.get_refresh_token(refresh_token)

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        now = int(time.time())
        new_pair_id = secrets.token_urlsafe(16)
        new_access_str = secrets.token_urlsafe(32)
        new_refresh_str = secrets.token_urlsafe(32)

        new_access = AccessToken(
            token=new_access_str,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + ACCESS_TOKEN_TTL,
            resource=None,
        )
        new_refresh = RefreshToken(
            token=new_refresh_str,
            client_id=client.client_id,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_TTL,
        )

        try:
            await self.store.save_access_token(new_access, new_pair_id)
            await self.store.save_refresh_token(new_refresh, new_pair_id)
        except Exception as e:
            logger.exception("Failed to save tokens during refresh token exchange")
            raise TokenError(error="server_error", error_description="internal error") from e

        # Clean up old tokens (best-effort — new tokens are already persisted)
        try:
            await self.store.delete_paired_tokens(refresh_token.token, "refresh_token")
            await self.store.delete_token(f"refresh_token:{refresh_token.token}")
        except Exception:
            logger.exception("Failed to clean up old tokens during refresh")

        return OAuthToken(
            access_token=new_access_str,
            token_type="Bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=new_refresh_str,
            scope=" ".join(scopes) if scopes else None,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        # Try CouchDB first
        result = await self.store.get_access_token(token)
        if result is not None:
            return result
        # Fallback to static API key
        if self.api_key and token == self.api_key:
            return AccessToken(token=token, client_id="api-key", scopes=[], expires_at=None)
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke a token and its paired counterpart (cascading revocation)."""
        if isinstance(token, AccessToken):
            token_type = "access_token"
            doc_id = f"access_token:{token.token}"
        else:
            token_type = "refresh_token"
            doc_id = f"refresh_token:{token.token}"

        await self.store.delete_paired_tokens(token.token, token_type)
        await self.store.delete_token(doc_id)

    # ── Helpers ────────────────────────────────────────────────────

    @property
    def _callback_url(self) -> str:
        return f"{self.resource_url}{self.config.oauth_callback_path}"
