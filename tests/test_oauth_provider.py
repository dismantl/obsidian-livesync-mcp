"""Tests for OIDCDelegatingProvider."""

import time
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull

from obsidian_livesync_mcp.config import Config
from obsidian_livesync_mcp.oauth_provider import OIDCDelegatingProvider
from obsidian_livesync_mcp.oauth_store import OAuthStore

ISSUER_URL = "https://auth.example.com"
DISCOVERY_URL = f"{ISSUER_URL}/.well-known/openid-configuration"
JWKS_URI = f"{ISSUER_URL}/.well-known/jwks.json"

DISCOVERY_DOC = {
    "issuer": ISSUER_URL,
    "authorization_endpoint": f"{ISSUER_URL}/authorize",
    "token_endpoint": f"{ISSUER_URL}/token",
    "jwks_uri": JWKS_URI,
}

MOCK_JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "kid": "test-key-1",
            "alg": "RS256",
            "n": "0vx7agoebGcQSuu...",
            "e": "AQAB",
        }
    ]
}


@pytest.fixture
def config():
    return Config(
        couch_url="http://localhost:5984",
        couch_user="admin",
        couch_pass="password",
        oauth_issuer_url=ISSUER_URL,
        oauth_client_id="mcp-client-id",
        oauth_client_secret="mcp-client-secret",
        oauth_authorized_email="test@example.com",
    )


@pytest.fixture
def mock_store():
    store = AsyncMock(spec=OAuthStore)
    store._client = AsyncMock()
    return store


@pytest.fixture
def provider(config, mock_store):
    http_client = httpx.AsyncClient()
    p = OIDCDelegatingProvider(
        config=config,
        store=mock_store,
        http_client=http_client,
        resource_url="https://mcp.example.com",
        api_key="static-key-123",
    )
    # Pre-populate discovery data (skip actual HTTP fetch)
    p._authorization_endpoint = DISCOVERY_DOC["authorization_endpoint"]
    p._token_endpoint = DISCOVERY_DOC["token_endpoint"]
    p._jwks_uri = JWKS_URI
    p._jwks = MOCK_JWKS
    return p


@pytest.fixture
def sample_client():
    return OAuthClientInformationFull(
        client_id="claude-client-1",
        client_secret="claude-secret",
        redirect_uris=["https://claude.ai/oauth/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
    )


# ── initialize ────────────────────────────────────────────────────


async def test_initialize_fetches_discovery_and_jwks():
    config = Config(
        couch_url="http://localhost:5984",
        oauth_issuer_url=ISSUER_URL,
        oauth_client_id="id",
        oauth_client_secret="secret",
        oauth_authorized_email="test@example.com",
    )
    store = AsyncMock(spec=OAuthStore)
    store._client = AsyncMock()

    with respx.mock:
        respx.get(DISCOVERY_URL).mock(return_value=httpx.Response(200, json=DISCOVERY_DOC))
        respx.get(JWKS_URI).mock(return_value=httpx.Response(200, json=MOCK_JWKS))

        http_client = httpx.AsyncClient()
        p = OIDCDelegatingProvider(
            config=config,
            store=store,
            http_client=http_client,
            resource_url="https://mcp.example.com",
        )
        await p.initialize()

        assert p._authorization_endpoint == f"{ISSUER_URL}/authorize"
        assert p._token_endpoint == f"{ISSUER_URL}/token"
        assert p._jwks == MOCK_JWKS
        await http_client.aclose()


async def test_initialize_fails_on_bad_discovery():
    config = Config(
        couch_url="http://localhost:5984",
        oauth_issuer_url=ISSUER_URL,
        oauth_client_id="id",
        oauth_client_secret="secret",
        oauth_authorized_email="test@example.com",
    )
    store = AsyncMock(spec=OAuthStore)
    store._client = AsyncMock()

    with respx.mock:
        respx.get(DISCOVERY_URL).mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        http_client = httpx.AsyncClient()
        p = OIDCDelegatingProvider(
            config=config,
            store=store,
            http_client=http_client,
            resource_url="https://mcp.example.com",
        )

        with pytest.raises(RuntimeError, match="OIDC discovery failed"):
            await p.initialize()
        await http_client.aclose()


# ── get_client / register_client ──────────────────────────────────


async def test_get_client_delegates_to_store(provider, mock_store, sample_client):
    mock_store.get_client.return_value = sample_client
    result = await provider.get_client("claude-client-1")
    assert result == sample_client
    mock_store.get_client.assert_called_once_with("claude-client-1")


async def test_register_client_delegates_to_store(provider, mock_store, sample_client):
    await provider.register_client(sample_client)
    mock_store.save_client.assert_called_once_with(sample_client)


# ── authorize ─────────────────────────────────────────────────────


async def test_authorize_returns_oidc_redirect(provider, sample_client):
    params = AuthorizationParams(
        state="client-state-123",
        scopes=["openid"],
        code_challenge="challenge_value",
        redirect_uri="https://claude.ai/oauth/callback",
        redirect_uri_provided_explicitly=True,
        resource=None,
    )

    url = await provider.authorize(sample_client, params)

    assert url.startswith(f"{ISSUER_URL}/authorize?")
    assert "client_id=mcp-client-id" in url
    assert "response_type=code" in url
    assert "scope=openid+email+profile" in url


async def test_authorize_forwards_pkce_to_oidc_provider(provider, sample_client):
    params = AuthorizationParams(
        state="client-state-123",
        scopes=["openid"],
        code_challenge="challenge_value",
        redirect_uri="https://claude.ai/oauth/callback",
        redirect_uri_provided_explicitly=True,
        resource=None,
    )

    url = await provider.authorize(sample_client, params)

    assert "code_challenge=challenge_value" in url
    assert "code_challenge_method=S256" in url


async def test_authorize_stores_state_mapping(provider, sample_client):
    params = AuthorizationParams(
        state="client-state",
        scopes=[],
        code_challenge="test_challenge",
        redirect_uri="https://claude.ai/callback",
        redirect_uri_provided_explicitly=True,
        resource=None,
    )

    url = await provider.authorize(sample_client, params)

    # Extract state from the URL
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    internal_state = qs["state"][0]

    # Verify state mapping was stored
    stored = provider.ephemeral.get(f"state:{internal_state}")
    assert stored is not None
    assert stored["original_state"] == "client-state"
    assert stored["code_challenge"] == "test_challenge"
    assert stored["client_id"] == "claude-client-1"


# ── load/exchange authorization code ──────────────────────────────


async def test_load_authorization_code(provider, sample_client):
    # Manually store a code in ephemeral store
    provider.ephemeral.save(
        "code:test-code",
        {
            "client_id": "claude-client-1",
            "scopes": [],
            "code_challenge": "challenge",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_provided_explicitly": True,
            "resource": None,
            "expires_at": time.time() + 600,
        },
    )

    result = await provider.load_authorization_code(sample_client, "test-code")
    assert result is not None
    assert result.code == "test-code"
    assert result.client_id == "claude-client-1"


async def test_load_expired_authorization_code_returns_none(provider, sample_client):
    provider.ephemeral.save(
        "code:old-code",
        {
            "client_id": "claude-client-1",
            "scopes": [],
            "code_challenge": "challenge",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_provided_explicitly": True,
            "resource": None,
            "expires_at": time.time() - 1,
        },
    )

    result = await provider.load_authorization_code(sample_client, "old-code")
    assert result is None


async def test_exchange_authorization_code_returns_tokens(provider, mock_store, sample_client):
    auth_code = AuthorizationCode(
        code="test-code",
        scopes=[],
        expires_at=time.time() + 600,
        client_id="claude-client-1",
        code_challenge="challenge",
        redirect_uri="https://claude.ai/callback",
        redirect_uri_provided_explicitly=True,
        resource=None,
    )

    result = await provider.exchange_authorization_code(sample_client, auth_code)

    assert result.access_token
    assert result.refresh_token
    assert result.token_type == "Bearer"
    assert result.expires_in == 3600
    # Verify tokens were stored
    assert mock_store.save_access_token.called
    assert mock_store.save_refresh_token.called


# ── load/exchange refresh token ───────────────────────────────────


async def test_load_refresh_token_delegates_to_store(provider, mock_store, sample_client):
    mock_token = RefreshToken(
        token="ref_123", client_id="claude-client-1", scopes=[], expires_at=int(time.time()) + 86400
    )
    mock_store.get_refresh_token.return_value = mock_token

    result = await provider.load_refresh_token(sample_client, "ref_123")
    assert result == mock_token


async def test_exchange_refresh_token_rotates_tokens(provider, mock_store, sample_client):
    old_refresh = RefreshToken(
        token="old_ref", client_id="claude-client-1", scopes=[], expires_at=int(time.time()) + 86400
    )

    result = await provider.exchange_refresh_token(sample_client, old_refresh, [])

    assert result.access_token
    assert result.refresh_token
    assert result.access_token != "old_tok"
    assert result.refresh_token != "old_ref"
    # Verify new tokens stored and old tokens cleaned up
    assert mock_store.save_access_token.called
    assert mock_store.save_refresh_token.called
    mock_store.delete_paired_tokens.assert_called_once_with("old_ref", "refresh_token")
    mock_store.delete_token.assert_called_once_with("refresh_token:old_ref")


# ── load_access_token ─────────────────────────────────────────────


async def test_load_access_token_from_store(provider, mock_store):
    mock_token = AccessToken(
        token="tok_123", client_id="client1", scopes=[], expires_at=int(time.time()) + 3600
    )
    mock_store.get_access_token.return_value = mock_token

    result = await provider.load_access_token("tok_123")
    assert result == mock_token


async def test_load_access_token_falls_back_to_api_key(provider, mock_store):
    mock_store.get_access_token.return_value = None

    result = await provider.load_access_token("static-key-123")
    assert result is not None
    assert result.client_id == "api-key"
    assert result.token == "static-key-123"


async def test_load_access_token_unknown_returns_none(provider, mock_store):
    mock_store.get_access_token.return_value = None

    result = await provider.load_access_token("unknown-token")
    assert result is None


# ── revoke_token ──────────────────────────────────────────────────


async def test_revoke_cascades_deletion(provider, mock_store):
    token = AccessToken(
        token="tok_to_revoke", client_id="client1", scopes=[], expires_at=int(time.time()) + 3600
    )

    await provider.revoke_token(token)

    # Should delete paired tokens and then the token itself
    mock_store.delete_paired_tokens.assert_called_once_with("tok_to_revoke", "access_token")
    mock_store.delete_token.assert_called_once_with("access_token:tok_to_revoke")
