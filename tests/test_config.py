"""Tests for obsidian_livesync_mcp.config — validation and repr."""

import pytest

from obsidian_livesync_mcp.config import Config


def test_config_valid():
    c = Config(couch_url="http://localhost:5984", couch_user="u", couch_pass="p")
    assert c.couch_url == "http://localhost:5984"
    assert c.db_name == "obsidian-vault"


def test_config_empty_url_raises():
    with pytest.raises(ValueError, match="CouchDB URL is required"):
        Config(couch_url="")


def test_config_no_url_raises(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_COUCH_URL", raising=False)
    monkeypatch.delenv("COUCHDB_URL", raising=False)
    with pytest.raises(ValueError, match="CouchDB URL is required"):
        Config(couch_url="")


def test_config_db_url():
    c = Config(couch_url="http://host:5984", db_name="my-vault")
    assert c.db_url == "http://host:5984/my-vault"


def test_config_default_db_name():
    c = Config(couch_url="http://host:5984")
    assert c.db_name == "obsidian-vault"


def test_link_config_defaults():
    c = Config(couch_url="http://host:5984")
    assert c.link_ttl_seconds == 300
    assert c.max_upload_bytes == 100 * 1024 * 1024
    assert c.max_concurrent_transfers == 2


def test_link_config_env_overrides(monkeypatch):
    monkeypatch.setenv("MCP_LINK_TTL_SECONDS", "120")
    monkeypatch.setenv("MCP_MAX_UPLOAD_BYTES", "2048")
    monkeypatch.setenv("MCP_MAX_CONCURRENT_TRANSFERS", "4")

    c = Config(couch_url="http://host:5984")

    assert c.link_ttl_seconds == 120
    assert c.max_upload_bytes == 2048
    assert c.max_concurrent_transfers == 4


def test_config_repr_masks_password():
    c = Config(couch_url="http://x", couch_user="admin", couch_pass="s3cret")
    r = repr(c)
    assert "s3cret" not in r
    assert "***" in r
    assert "http://x" in r
    assert "admin" in r


def test_config_frozen():
    c = Config(couch_url="http://x")
    with pytest.raises(AttributeError):
        c.couch_url = "http://other"
