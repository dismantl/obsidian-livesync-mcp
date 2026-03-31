"""Tests for OAuthStore (CouchDB-backed OAuth storage)."""

import time

import httpx
import pytest
import respx
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from obsidian_self_mcp.oauth_store import DESIGN_DOC, OAUTH_DB_NAME, OAuthStore

COUCH_URL = "http://localhost:5984"
BASE_URL = f"{COUCH_URL}/{OAUTH_DB_NAME}"


@pytest.fixture
def mock_couch():
    with respx.mock(base_url=BASE_URL) as mock:
        yield mock


@pytest.fixture
def store():
    return OAuthStore(
        couch_url=COUCH_URL,
        couch_user="admin",
        couch_pass="password",
    )


# ── ensure_db ─────────────────────────────────────────────────────


async def test_ensure_db_creates_database_and_design_doc(store):
    with respx.mock:
        # Database creation
        respx.put(f"{COUCH_URL}/{OAUTH_DB_NAME}").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )
        # Design doc check (not found)
        respx.get(f"{BASE_URL}/_design/oauth").mock(return_value=httpx.Response(404))
        # Design doc creation
        respx.put(f"{BASE_URL}/_design/oauth").mock(
            return_value=httpx.Response(201, json={"ok": True})
        )

        await store.ensure_db()


async def test_ensure_db_skips_existing_database(store):
    with respx.mock:
        # Database already exists
        respx.put(f"{COUCH_URL}/{OAUTH_DB_NAME}").mock(
            return_value=httpx.Response(412, json={"error": "file_exists"})
        )
        # Design doc exists with same views
        respx.get(f"{BASE_URL}/_design/oauth").mock(
            return_value=httpx.Response(
                200,
                json={
                    "_rev": "1-abc",
                    "views": DESIGN_DOC["views"],
                },
            )
        )

        await store.ensure_db()


# ── Client CRUD ───────────────────────────────────────────────────


async def test_save_and_get_client(store, mock_couch):
    client_info = OAuthClientInformationFull(
        client_id="test-client",
        client_secret="test-secret",
        redirect_uris=["https://example.com/callback"],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="client_secret_post",
    )

    # Save: check for existing (not found), then put
    mock_couch.get("/client:test-client").mock(return_value=httpx.Response(404))
    mock_couch.put("/client:test-client").mock(return_value=httpx.Response(201, json={"ok": True}))
    await store.save_client(client_info)

    # Get: return the doc
    mock_couch.get("/client:test-client").mock(
        return_value=httpx.Response(
            200,
            json={
                "_id": "client:test-client",
                "_rev": "1-abc",
                "type": "client",
                "client_id": "test-client",
                "client_secret": "test-secret",
                "redirect_uris": ["https://example.com/callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
    )
    result = await store.get_client("test-client")
    assert result is not None
    assert result.client_id == "test-client"


async def test_get_client_not_found(store, mock_couch):
    mock_couch.get("/client:unknown").mock(return_value=httpx.Response(404))
    result = await store.get_client("unknown")
    assert result is None


# ── Access Token CRUD ─────────────────────────────────────────────


async def test_save_and_get_access_token(store, mock_couch):
    token = AccessToken(
        token="tok_abc",
        client_id="client1",
        scopes=[],
        expires_at=int(time.time()) + 3600,
    )

    mock_couch.put("/access_token:tok_abc").mock(
        return_value=httpx.Response(201, json={"ok": True})
    )
    await store.save_access_token(token, "pair1")

    mock_couch.get("/access_token:tok_abc").mock(
        return_value=httpx.Response(
            200,
            json={
                "_id": "access_token:tok_abc",
                "_rev": "1-abc",
                "type": "access_token",
                "token": "tok_abc",
                "client_id": "client1",
                "scopes": [],
                "expires_at": int(time.time()) + 3600,
                "token_pair_id": "pair1",
            },
        )
    )
    result = await store.get_access_token("tok_abc")
    assert result is not None
    assert result.token == "tok_abc"


async def test_get_expired_access_token_returns_none(store, mock_couch):
    expired_time = int(time.time()) - 1

    mock_couch.get("/access_token:tok_old").mock(
        return_value=httpx.Response(
            200,
            json={
                "_id": "access_token:tok_old",
                "_rev": "1-abc",
                "type": "access_token",
                "token": "tok_old",
                "client_id": "client1",
                "scopes": [],
                "expires_at": expired_time,
                "token_pair_id": "pair1",
            },
        )
    )
    mock_couch.delete("/access_token:tok_old").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    result = await store.get_access_token("tok_old")
    assert result is None


# ── Refresh Token CRUD ────────────────────────────────────────────


async def test_save_and_get_refresh_token(store, mock_couch):
    token = RefreshToken(
        token="ref_abc",
        client_id="client1",
        scopes=[],
        expires_at=int(time.time()) + 86400,
    )

    mock_couch.put("/refresh_token:ref_abc").mock(
        return_value=httpx.Response(201, json={"ok": True})
    )
    await store.save_refresh_token(token, "pair1")

    mock_couch.get("/refresh_token:ref_abc").mock(
        return_value=httpx.Response(
            200,
            json={
                "_id": "refresh_token:ref_abc",
                "_rev": "1-abc",
                "type": "refresh_token",
                "token": "ref_abc",
                "client_id": "client1",
                "scopes": [],
                "expires_at": int(time.time()) + 86400,
                "token_pair_id": "pair1",
            },
        )
    )
    result = await store.get_refresh_token("ref_abc")
    assert result is not None
    assert result.token == "ref_abc"


# ── Cascading Revocation ──────────────────────────────────────────


async def test_get_tokens_by_pair_id(store, mock_couch):
    mock_couch.get("/_design/oauth/_view/by_token_pair").mock(
        return_value=httpx.Response(
            200,
            json={
                "rows": [
                    {"doc": {"_id": "access_token:tok1", "_rev": "1-a", "token_pair_id": "pair1"}},
                    {"doc": {"_id": "refresh_token:ref1", "_rev": "1-b", "token_pair_id": "pair1"}},
                ]
            },
        )
    )
    results = await store.get_tokens_by_pair_id("pair1")
    assert len(results) == 2


async def test_delete_token(store, mock_couch):
    mock_couch.get("/access_token:tok1").mock(
        return_value=httpx.Response(200, json={"_id": "access_token:tok1", "_rev": "1-abc"})
    )
    mock_couch.delete("/access_token:tok1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    await store.delete_token("access_token:tok1")


async def test_delete_token_not_found_is_noop(store, mock_couch):
    mock_couch.get("/access_token:missing").mock(return_value=httpx.Response(404))
    await store.delete_token("access_token:missing")


# ── Background Purge ──────────────────────────────────────────────


async def test_purge_expired(store, mock_couch):
    now = int(time.time())
    mock_couch.get("/_design/oauth/_view/by_expiry").mock(
        return_value=httpx.Response(
            200,
            json={
                "rows": [
                    {"doc": {"_id": "access_token:old1", "_rev": "1-a", "expires_at": now - 100}},
                    {"doc": {"_id": "refresh_token:old2", "_rev": "1-b", "expires_at": now - 50}},
                ]
            },
        )
    )
    mock_couch.delete("/access_token:old1").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    mock_couch.delete("/refresh_token:old2").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    deleted = await store.purge_expired()
    assert deleted == 2
