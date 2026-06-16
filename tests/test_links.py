"""Tests for ephemeral capability URL tokens."""

from obsidian_livesync_mcp.links import EphemeralLinkStore


def test_download_link_is_reusable_within_ttl():
    now = 1000.0
    store = EphemeralLinkStore(now=lambda: now, token_factory=lambda: "download-token")

    record = store.create(
        vault_path="Attachments/a.png",
        mode="download",
        ttl_seconds=60,
    )

    first, first_status = store.resolve(record.token, mode="download")
    second, second_status = store.resolve(record.token, mode="download")

    assert first_status == "ok"
    assert second_status == "ok"
    assert first == second == record


def test_upload_link_can_be_consumed_once():
    now = 1000.0
    store = EphemeralLinkStore(now=lambda: now, token_factory=lambda: "upload-token")
    record = store.create(
        vault_path="Attachments/a.png",
        mode="upload",
        ttl_seconds=60,
        max_bytes=1024,
    )

    first, first_status = store.resolve(record.token, mode="upload", consume=True)
    second, second_status = store.resolve(record.token, mode="upload", consume=True)

    assert first_status == "ok"
    assert first == record
    assert second_status == "missing"
    assert second is None


def test_expired_link_is_removed_and_reported():
    now = 1000.0
    store = EphemeralLinkStore(now=lambda: now, token_factory=lambda: "expired-token")
    record = store.create(vault_path="a.md", mode="download", ttl_seconds=10)
    now = 1011.0

    resolved, status = store.resolve(record.token, mode="download")

    assert resolved is None
    assert status == "expired"
    assert store.resolve(record.token, mode="download")[1] == "missing"


def test_wrong_mode_does_not_consume_link():
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "token")
    record = store.create(vault_path="a.md", mode="download", ttl_seconds=10)

    wrong, wrong_status = store.resolve(record.token, mode="upload", consume=True)
    right, right_status = store.resolve(record.token, mode="download")

    assert wrong is None
    assert wrong_status == "wrong_mode"
    assert right_status == "ok"
    assert right == record


def test_generated_tokens_are_high_entropy_and_unique():
    store = EphemeralLinkStore()

    tokens = {
        store.create(vault_path=f"file-{index}.bin", mode="download", ttl_seconds=60).token
        for index in range(20)
    }

    assert len(tokens) == 20
    assert all(len(token) >= 40 for token in tokens)
