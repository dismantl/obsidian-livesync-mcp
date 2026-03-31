"""Tests for the EphemeralStore (in-memory auth codes and state mappings)."""

import time

import pytest

from obsidian_self_mcp.oauth_provider import EphemeralStore


@pytest.fixture
def store():
    return EphemeralStore()


def test_save_and_get(store):
    store.save("key1", {"data": "value", "expires_at": time.time() + 60})
    assert store.get("key1")["data"] == "value"


def test_get_missing_returns_none(store):
    assert store.get("nonexistent") is None


def test_expired_entry_returns_none(store):
    store.save("key1", {"data": "value", "expires_at": time.time() - 1})
    assert store.get("key1") is None


def test_pop_returns_and_deletes(store):
    store.save("key1", {"data": "value", "expires_at": time.time() + 60})
    result = store.pop("key1")
    assert result["data"] == "value"
    assert store.get("key1") is None


def test_pop_missing_returns_none(store):
    assert store.pop("nonexistent") is None


def test_pop_expired_returns_none(store):
    store.save("key1", {"data": "value", "expires_at": time.time() - 1})
    assert store.pop("key1") is None


def test_sweep_on_save(store):
    store.save("old", {"expires_at": time.time() - 1})
    store.save("new", {"expires_at": time.time() + 60})
    # old should have been swept
    assert store.get("old") is None
    assert store.get("new") is not None


def test_no_expires_at_treated_as_infinite(store):
    store.save("key1", {"data": "no-expiry"})
    assert store.get("key1")["data"] == "no-expiry"
