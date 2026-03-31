"""Tests for OAuth-related Config changes."""

import pytest

from obsidian_self_mcp.config import Config


def test_oauth_disabled_by_default():
    c = Config(couch_url="http://localhost:5984")
    assert c.oauth_enabled is False
    assert c.oauth_issuer_url is None


def test_oauth_enabled_with_all_vars():
    c = Config(
        couch_url="http://localhost:5984",
        oauth_issuer_url="https://auth.example.com",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        oauth_authorized_email="user@example.com",
    )
    assert c.oauth_enabled is True
    assert c.oauth_issuer_url == "https://auth.example.com"


def test_oauth_issuer_without_client_id_raises():
    with pytest.raises(ValueError, match="OAUTH_CLIENT_ID is required"):
        Config(
            couch_url="http://localhost:5984",
            oauth_issuer_url="https://auth.example.com",
            oauth_client_secret="secret",
            oauth_authorized_email="user@example.com",
        )


def test_oauth_issuer_without_client_secret_raises():
    with pytest.raises(ValueError, match="OAUTH_CLIENT_SECRET is required"):
        Config(
            couch_url="http://localhost:5984",
            oauth_issuer_url="https://auth.example.com",
            oauth_client_id="client-id",
            oauth_authorized_email="user@example.com",
        )


def test_oauth_issuer_without_authorized_email_raises():
    with pytest.raises(ValueError, match="OAUTH_AUTHORIZED_EMAIL is required"):
        Config(
            couch_url="http://localhost:5984",
            oauth_issuer_url="https://auth.example.com",
            oauth_client_id="client-id",
            oauth_client_secret="secret",
        )


def test_oauth_callback_path():
    c = Config(couch_url="http://localhost:5984")
    assert c.oauth_callback_path == "/oauth/callback"


def test_oauth_authorized_email():
    c = Config(
        couch_url="http://localhost:5984",
        oauth_issuer_url="https://auth.example.com",
        oauth_client_id="id",
        oauth_client_secret="secret",
        oauth_authorized_email="user@example.com",
    )
    assert c.oauth_authorized_email == "user@example.com"


def test_repr_includes_oauth_issuer():
    c = Config(
        couch_url="http://localhost:5984",
        oauth_issuer_url="https://auth.example.com",
        oauth_client_id="id",
        oauth_client_secret="my-secret-value",
        oauth_authorized_email="user@example.com",
    )
    r = repr(c)
    assert "oauth_issuer_url='https://auth.example.com'" in r
    # Secrets should not be in repr
    assert "my-secret-value" not in r
    assert "oauth_client_secret='***'" in r
