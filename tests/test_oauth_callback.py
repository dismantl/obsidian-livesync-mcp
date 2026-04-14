"""Tests for the OAuth callback route handler."""

import time
from base64 import b64decode
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from urllib.parse import unquote_plus

from obsidian_livesync_mcp.config import Config
from obsidian_livesync_mcp.oauth_callback import _validate_id_token, handle_oauth_callback
from obsidian_livesync_mcp.oauth_provider import OIDCDelegatingProvider
from obsidian_livesync_mcp.oauth_store import OAuthStore

ISSUER_URL = "https://auth.example.com"


def _generate_rsa_keypair():
    """Generate an RSA key pair for testing JWT signing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


def _make_jwks(public_key, kid="test-key-1"):
    """Build a JWKS dict from a public key."""
    from jwt.algorithms import RSAAlgorithm

    jwk = RSAAlgorithm.to_jwk(public_key, as_dict=True)
    jwk["kid"] = kid
    jwk["alg"] = "RS256"
    jwk["use"] = "sig"
    return {"keys": [jwk]}


def _make_id_token(private_key, claims, kid="test-key-1"):
    """Sign an ID token JWT with the test private key."""
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def rsa_keys():
    private_key, public_key = _generate_rsa_keypair()
    return private_key, public_key


@pytest.fixture
def provider(rsa_keys):
    _, public_key = rsa_keys
    config = Config(
        couch_url="http://localhost:5984",
        oauth_issuer_url=ISSUER_URL,
        oauth_client_id="mcp-client-id",
        oauth_client_secret="mcp-client-secret",
        oauth_authorized_email="user@example.com",
    )
    store = AsyncMock(spec=OAuthStore)
    store._client = AsyncMock()
    http_client = httpx.AsyncClient()

    p = OIDCDelegatingProvider(
        config=config,
        store=store,
        http_client=http_client,
        resource_url="https://mcp.example.com",
    )
    p._authorization_endpoint = f"{ISSUER_URL}/authorize"
    p._token_endpoint = f"{ISSUER_URL}/token"
    p._jwks_uri = f"{ISSUER_URL}/.well-known/jwks.json"
    p._jwks = _make_jwks(public_key)
    return p


# ── _validate_id_token ────────────────────────────────────────────


async def test_validate_id_token_success(provider, rsa_keys):
    private_key, _ = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "mcp-client-id",
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    token = _make_id_token(private_key, claims)

    result = await _validate_id_token(token, provider)
    assert result is not None
    assert result["email"] == "user@example.com"


async def test_validate_id_token_expired(provider, rsa_keys):
    private_key, _ = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "mcp-client-id",
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": int(time.time()) - 100,
        "iat": int(time.time()) - 400,
    }
    token = _make_id_token(private_key, claims)

    result = await _validate_id_token(token, provider)
    assert result is None


async def test_validate_id_token_wrong_issuer(provider, rsa_keys):
    private_key, _ = rsa_keys
    claims = {
        "iss": "https://wrong-issuer.com",
        "aud": "mcp-client-id",
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    token = _make_id_token(private_key, claims)

    result = await _validate_id_token(token, provider)
    assert result is None


async def test_validate_id_token_wrong_audience(provider, rsa_keys):
    private_key, _ = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "wrong-client-id",
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    token = _make_id_token(private_key, claims)

    result = await _validate_id_token(token, provider)
    assert result is None


async def test_validate_id_token_unknown_kid_retries_jwks(provider, rsa_keys):
    private_key, public_key = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "mcp-client-id",
        "sub": "user-123",
        "email": "user@example.com",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    claims["email_verified"] = True
    # Sign with a different key ID
    token = _make_id_token(private_key, claims, kid="new-key-2")

    # Initial JWKS has old key, refresh will return new key
    new_jwks = _make_jwks(public_key, kid="new-key-2")

    with respx.mock:
        respx.get(provider._jwks_uri).mock(return_value=httpx.Response(200, json=new_jwks))
        result = await _validate_id_token(token, provider)

    assert result is not None
    assert result["email"] == "user@example.com"


# ── handle_oauth_callback ─────────────────────────────────────────


async def test_callback_invalid_state_returns_400(provider):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    async def endpoint(request):
        return await handle_oauth_callback(request, provider)

    app = Starlette(routes=[Route("/oauth/callback", endpoint)])
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/oauth/callback?code=test&state=bad-state")
    assert response.status_code == 400


async def test_callback_oidc_error_forwarded(provider):
    from starlette.applications import Starlette
    from starlette.routing import Route

    # Store a valid state mapping
    provider.ephemeral.save(
        "state:valid-state",
        {
            "original_state": "client-state",
            "code_challenge": "challenge",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_provided_explicitly": True,
            "scopes": [],
            "resource": None,
            "client_id": "client1",
            "expires_at": time.time() + 600,
        },
    )

    async def endpoint(request):
        return await handle_oauth_callback(request, provider)

    app = Starlette(routes=[Route("/oauth/callback", endpoint)])
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    url = "/oauth/callback?error=access_denied&error_description=user+denied&state=valid-state"
    response = client.get(url)
    assert response.status_code == 302
    assert "error=access_denied" in response.headers["location"]
    assert "claude.ai/callback" in response.headers["location"]


async def test_callback_success_redirects_with_code(provider, rsa_keys):
    from starlette.applications import Starlette
    from starlette.routing import Route

    private_key, _ = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "mcp-client-id",
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    id_token = _make_id_token(private_key, claims)

    provider.ephemeral.save(
        "state:valid-state",
        {
            "original_state": "client-state",
            "code_challenge": "challenge",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_provided_explicitly": True,
            "scopes": [],
            "resource": None,
            "client_id": "client1",
            "expires_at": time.time() + 600,
        },
    )

    async def endpoint(request):
        return await handle_oauth_callback(request, provider)

    app = Starlette(routes=[Route("/oauth/callback", endpoint)])
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    with respx.mock:
        respx.post(f"{ISSUER_URL}/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "oidc_access",
                    "id_token": id_token,
                    "token_type": "Bearer",
                },
            )
        )

        response = client.get("/oauth/callback?code=oidc-code&state=valid-state")

    assert response.status_code == 302
    location = response.headers["location"]
    assert location.startswith("https://claude.ai/callback?")
    assert "code=" in location
    assert "state=client-state" in location


async def test_callback_uses_client_secret_basic_for_token_exchange(provider, rsa_keys):
    from starlette.applications import Starlette
    from starlette.routing import Route

    private_key, _ = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "mcp-client-id",
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    id_token = _make_id_token(private_key, claims)

    provider.ephemeral.save(
        "state:valid-state",
        {
            "original_state": "client-state",
            "code_challenge": "challenge",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_provided_explicitly": True,
            "scopes": [],
            "resource": None,
            "client_id": "client1",
            "expires_at": time.time() + 600,
        },
    )

    async def endpoint(request):
        return await handle_oauth_callback(request, provider)

    app = Starlette(routes=[Route("/oauth/callback", endpoint)])
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    captured = {}

    def token_handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "oidc_access",
                "id_token": id_token,
                "token_type": "Bearer",
            },
        )

    with respx.mock:
        respx.post(f"{ISSUER_URL}/token").mock(side_effect=token_handler)
        response = client.get("/oauth/callback?code=oidc-code&state=valid-state")

    assert response.status_code == 302
    assert captured["authorization"] == "Basic bWNwLWNsaWVudC1pZDptY3AtY2xpZW50LXNlY3JldA=="
    assert "client_id=" not in captured["body"]
    assert "client_secret=" not in captured["body"]


async def test_callback_urlencodes_basic_auth_credentials(provider, rsa_keys):
    from starlette.applications import Starlette
    from starlette.routing import Route

    private_key, _ = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "client+id",
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    id_token = _make_id_token(private_key, claims)

    provider.config = Config(
        couch_url="http://localhost:5984",
        oauth_issuer_url=ISSUER_URL,
        oauth_client_id="client+id",
        oauth_client_secret="secret+value",
        oauth_authorized_email="user@example.com",
    )

    provider.ephemeral.save(
        "state:valid-state",
        {
            "original_state": "client-state",
            "code_challenge": "challenge",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_provided_explicitly": True,
            "scopes": [],
            "resource": None,
            "client_id": "client1",
            "expires_at": time.time() + 600,
        },
    )

    async def endpoint(request):
        return await handle_oauth_callback(request, provider)

    app = Starlette(routes=[Route("/oauth/callback", endpoint)])
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    captured = {}

    def token_handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "access_token": "oidc_access",
                "id_token": id_token,
                "token_type": "Bearer",
            },
        )

    with respx.mock:
        respx.post(f"{ISSUER_URL}/token").mock(side_effect=token_handler)
        response = client.get("/oauth/callback?code=oidc-code&state=valid-state")

    assert response.status_code == 302
    scheme, encoded = captured["authorization"].split(" ", 1)
    assert scheme == "Basic"
    username, password = b64decode(encoded).decode().split(":", 1)
    assert username == "client%2Bid"
    assert password == "secret%2Bvalue"
    assert unquote_plus(username) == "client+id"
    assert unquote_plus(password) == "secret+value"


async def test_callback_does_not_depend_on_provider_http_client(provider, rsa_keys):
    from starlette.applications import Starlette
    from starlette.routing import Route

    private_key, _ = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "mcp-client-id",
        "sub": "user-123",
        "email": "user@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    id_token = _make_id_token(private_key, claims)

    provider.ephemeral.save(
        "state:valid-state",
        {
            "original_state": "client-state",
            "code_challenge": "challenge",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_provided_explicitly": True,
            "scopes": [],
            "resource": None,
            "client_id": "client1",
            "expires_at": time.time() + 600,
        },
    )

    async def endpoint(request):
        return await handle_oauth_callback(request, provider)

    app = Starlette(routes=[Route("/oauth/callback", endpoint)])
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    class BrokenClient:
        async def post(self, *args, **kwargs):
            raise AssertionError("callback should not use provider.http_client")

    provider.http_client = BrokenClient()

    with respx.mock:
        respx.post(f"{ISSUER_URL}/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "oidc_access",
                    "id_token": id_token,
                    "token_type": "Bearer",
                },
            )
        )

        response = client.get("/oauth/callback?code=oidc-code&state=valid-state")

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://claude.ai/callback?")


async def test_callback_unauthorized_email(provider, rsa_keys):
    from starlette.applications import Starlette
    from starlette.routing import Route

    private_key, _ = rsa_keys
    claims = {
        "iss": ISSUER_URL,
        "aud": "mcp-client-id",
        "sub": "user-456",
        "email": "unauthorized@example.com",
        "email_verified": True,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
    }
    id_token = _make_id_token(private_key, claims)

    provider.ephemeral.save(
        "state:valid-state",
        {
            "original_state": "client-state",
            "code_challenge": "challenge",
            "redirect_uri": "https://claude.ai/callback",
            "redirect_uri_provided_explicitly": True,
            "scopes": [],
            "resource": None,
            "client_id": "client1",
            "expires_at": time.time() + 600,
        },
    )

    async def endpoint(request):
        return await handle_oauth_callback(request, provider)

    app = Starlette(routes=[Route("/oauth/callback", endpoint)])
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)

    with respx.mock:
        respx.post(f"{ISSUER_URL}/token").mock(
            return_value=httpx.Response(
                200,
                json={
                    "access_token": "oidc_access",
                    "id_token": id_token,
                    "token_type": "Bearer",
                },
            )
        )

        response = client.get("/oauth/callback?code=oidc-code&state=valid-state")

    assert response.status_code == 302
    assert "error=access_denied" in response.headers["location"]
    assert "unauthorized+user" in response.headers["location"]
