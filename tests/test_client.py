"""Tests for obsidian_self_mcp.client — async CouchDB client with respx mocking."""

import pytest
import respx
from httpx import Response

from obsidian_self_mcp.client import ObsidianVaultClient
from obsidian_self_mcp.config import Config

BASE = "http://test:5984/test-vault"


@pytest.fixture
def config():
    return Config(
        couch_url="http://test:5984",
        couch_user="user",
        couch_pass="pass",
        db_name="test-vault",
    )


@pytest.fixture
def client(config):
    return ObsidianVaultClient(config)


def _make_parent_doc(doc_id, children, **kwargs):
    """Helper to build a CouchDB parent document."""
    doc = {
        "_id": doc_id,
        "_rev": "1-abc",
        "children": children,
        "type": "plain",
        "ctime": 1700000000000,
        "mtime": 1700000000000,
        "size": 100,
        "path": doc_id,
    }
    doc.update(kwargs)
    return doc


def _mock_get_doc(doc_id_encoded, doc):
    """Mock a successful GET for a document."""
    respx.get(f"{BASE}/{doc_id_encoded}").mock(return_value=Response(200, json=doc))


def _mock_get_doc_404(doc_id_encoded):
    """Mock a 404 GET for a document."""
    respx.get(f"{BASE}/{doc_id_encoded}").mock(
        return_value=Response(404, json={"error": "not_found"})
    )


def _mock_all_docs(chunks: dict[str, str]):
    """Mock POST _all_docs returning chunk data."""
    rows = [{"id": cid, "doc": {"data": data}} for cid, data in chunks.items()]
    respx.post(f"{BASE}/_all_docs").mock(return_value=Response(200, json={"rows": rows}))


def _mock_get_all_file_docs(docs: list[dict]):
    """Mock the two GET /_all_docs calls used by _get_all_file_docs."""
    respx.get(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(200, json={"rows": [{"doc": d} for d in docs]}),
            Response(200, json={"rows": []}),
        ]
    )


# ── _get_doc ──────────────────────────────────────────────────────


@respx.mock
async def test_get_doc_found(client):
    doc = _make_parent_doc("notes/todo.md", ["h:chunk1"])
    _mock_get_doc("notes%2Ftodo.md", doc)

    result = await client._get_doc("Notes/todo.md")
    assert result["_id"] == "notes/todo.md"


@respx.mock
async def test_get_doc_not_found(client):
    _mock_get_doc_404("notes%2Ftodo.md")
    _mock_get_doc_404("%2Fnotes%2Ftodo.md")

    result = await client._get_doc("Notes/todo.md")
    assert result is None


@respx.mock
async def test_get_doc_server_error_raises(client):
    respx.get(f"{BASE}/notes%2Ftodo.md").mock(
        return_value=Response(500, json={"error": "internal"})
    )
    with pytest.raises(Exception):
        await client._get_doc("Notes/todo.md")


@respx.mock
async def test_get_doc_underscore_prefix(client):
    """Paths starting with _ get / prefix for CouchDB."""
    doc = _make_parent_doc("/_changelog/entry.md", ["h:chunk1"])
    _mock_get_doc("%2F_changelog%2Fentry.md", doc)

    result = await client._get_doc("_Changelog/entry.md")
    assert result is not None


# ── read_note ─────────────────────────────────────────────────────


@respx.mock
async def test_read_note_single_chunk(client):
    doc = _make_parent_doc("notes/todo.md", ["h:abcdefghijkl"], size=12)
    _mock_get_doc("notes%2Ftodo.md", doc)
    _mock_all_docs({"h:abcdefghijkl": "Hello world!"})

    result = await client.read_note("Notes/todo.md")
    assert result is not None
    assert result.content == "Hello world!"
    assert result.path == "notes/todo.md"
    assert result.is_binary is False


@respx.mock
async def test_read_note_multiple_chunks(client):
    doc = _make_parent_doc("notes/long.md", ["h:chunk1aaaaaa", "h:chunk2bbbbbb"])
    _mock_get_doc("notes%2Flong.md", doc)
    _mock_all_docs({"h:chunk1aaaaaa": "First part. ", "h:chunk2bbbbbb": "Second part."})

    result = await client.read_note("Notes/long.md")
    assert result.content == "First part. Second part."


@respx.mock
async def test_read_note_not_found(client):
    _mock_get_doc_404("notes%2Fmissing.md")
    _mock_get_doc_404("%2Fnotes%2Fmissing.md")

    result = await client.read_note("Notes/missing.md")
    assert result is None


@respx.mock
async def test_read_note_missing_chunk_raises(client):
    doc = _make_parent_doc("notes/todo.md", ["h:exists00000", "h:missing00000"])
    _mock_get_doc("notes%2Ftodo.md", doc)
    # Only return one of the two chunks
    _mock_all_docs({"h:exists00000": "partial"})

    with pytest.raises(ValueError, match="Missing 1 chunk"):
        await client.read_note("Notes/todo.md")


@respx.mock
async def test_read_note_binary(client):
    doc = _make_parent_doc("img/photo.png", ["h:binchunk0000"], type="newnote")
    _mock_get_doc("img%2Fphoto.png", doc)
    _mock_all_docs({"h:binchunk0000": "aGVsbG8="})

    result = await client.read_note("img/photo.png")
    assert result.is_binary is True


# ── write_note ────────────────────────────────────────────────────


@respx.mock
async def test_write_note_new(client):
    # No existing doc
    _mock_get_doc_404("notes%2Fnew.md")
    _mock_get_doc_404("%2Fnotes%2Fnew.md")

    # Chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # Parent doc creation
    respx.put(f"{BASE}/notes%2Fnew.md").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-doc"})
    )

    result = await client.write_note("Notes/new.md", "# New Note")
    assert result is True


@respx.mock
async def test_write_note_update_existing(client):
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])
    _mock_get_doc("notes%2Ftodo.md", existing)
    # Reference scan for orphan cleanup: only this doc in the vault
    _mock_get_all_file_docs([existing])

    # Chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # Parent doc update
    respx.put(f"{BASE}/notes%2Ftodo.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-updated"})
    )

    result = await client.write_note("Notes/todo.md", "Updated content")
    assert result is True


@respx.mock
async def test_write_note_409_conflict_retry(client):
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])

    # First GET returns existing doc
    _mock_get_doc("notes%2Ftodo.md", existing)
    # Reference scan for orphan cleanup
    _mock_get_all_file_docs([existing])

    # Chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # First PUT returns 409, second succeeds
    respx.put(f"{BASE}/notes%2Ftodo.md").mock(
        side_effect=[
            Response(409, json={"error": "conflict"}),
            Response(200, json={"ok": True, "rev": "3-resolved"}),
        ]
    )
    # Refetch on conflict — need to mock the alternate ID too
    respx.get(f"{BASE}/%2Fnotes%2Ftodo.md").mock(
        return_value=Response(404, json={"error": "not_found"})
    )

    result = await client.write_note("Notes/todo.md", "Retried content")
    assert result is True


@respx.mock
async def test_write_note_409_deleted_during_write(client):
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])

    # First GET returns doc; second GET (refetch after 409) returns 404
    respx.get(f"{BASE}/notes%2Ftodo.md").mock(
        side_effect=[
            Response(200, json=existing),
            Response(404, json={"error": "not_found"}),
        ]
    )
    respx.get(f"{BASE}/%2Fnotes%2Ftodo.md").mock(
        return_value=Response(404, json={"error": "not_found"})
    )

    # Chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # PUT returns 409
    respx.put(f"{BASE}/notes%2Ftodo.md").mock(
        return_value=Response(409, json={"error": "conflict"})
    )

    with pytest.raises(ValueError, match="deleted during write"):
        await client.write_note("Notes/todo.md", "Content")


@respx.mock
async def test_write_note_preserves_shared_chunk(client):
    """Updating note A must not delete chunks still referenced by note B.

    Chunks are content-addressed and deduplicated across notes — two notes with
    identical content share the same chunk ID. Orphan cleanup that doesn't check
    cross-note references causes data loss in the other note.
    """
    shared_id = "h:shared000000"
    a_only_id = "h:aonly0000000"

    old_a = _make_parent_doc("notes/a.md", [shared_id, a_only_id])
    note_b = _make_parent_doc("notes/b.md", [shared_id])

    # write_note will GET the existing doc A
    _mock_get_doc("notes%2Fa.md", old_a)
    # Reference check after fix: returns both A and B so shared_id is seen as in-use
    _mock_get_all_file_docs([old_a, note_b])

    # New chunks for updated A content
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # Parent doc A update
    respx.put(f"{BASE}/notes%2Fa.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-updated"})
    )

    # Mock the orphan cleanup path for BOTH chunks so failures are visible
    respx.get(f"{BASE}/{shared_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"_id": shared_id, "_rev": "1-s"})
    )
    shared_delete = respx.delete(f"{BASE}/{shared_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"ok": True})
    )
    respx.get(f"{BASE}/{a_only_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"_id": a_only_id, "_rev": "1-a"})
    )
    a_only_delete = respx.delete(f"{BASE}/{a_only_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"ok": True})
    )

    result = await client.write_note("Notes/a.md", "completely different new content")
    assert result is True

    # The shared chunk must NOT be deleted — note B still references it
    assert not shared_delete.called, (
        "Shared chunk was deleted despite being referenced by another note — "
        "this is the dedup data-loss bug"
    )
    # The truly-orphaned chunk (only A referenced it) SHOULD still be cleaned up
    assert a_only_delete.called, "Truly-orphaned chunk was not cleaned up"


# ── append_note ───────────────────────────────────────────────────


@respx.mock
async def test_append_note(client):
    doc = _make_parent_doc("notes/log.md", ["h:lastchunk000"])
    _mock_get_doc("notes%2Flog.md", doc)
    _mock_all_docs({"h:lastchunk000": "existing content"})

    # New chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # Parent doc update
    respx.put(f"{BASE}/notes%2Flog.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-appended"})
    )

    result = await client.append_note("Notes/log.md", " appended")
    assert result is True


@respx.mock
async def test_append_note_not_found(client):
    _mock_get_doc_404("notes%2Fmissing.md")
    _mock_get_doc_404("%2Fnotes%2Fmissing.md")

    with pytest.raises(ValueError, match="Note not found"):
        await client.append_note("Notes/missing.md", "content")


@respx.mock
async def test_append_note_missing_last_chunk(client):
    doc = _make_parent_doc("notes/log.md", ["h:lastchunk000"])
    _mock_get_doc("notes%2Flog.md", doc)
    _mock_all_docs({})  # No chunks returned

    with pytest.raises(ValueError, match="Last chunk missing"):
        await client.append_note("Notes/log.md", " appended")


@respx.mock
async def test_append_note_409_conflict_retry(client):
    doc = _make_parent_doc("notes/log.md", ["h:lastchunk000"])
    _mock_get_doc("notes%2Flog.md", doc)
    _mock_all_docs({"h:lastchunk000": "existing"})

    # New chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # First PUT returns 409, second succeeds
    respx.put(f"{BASE}/notes%2Flog.md").mock(
        side_effect=[
            Response(409, json={"error": "conflict"}),
            Response(200, json={"ok": True, "rev": "3-resolved"}),
        ]
    )
    # Refetch on conflict
    respx.get(f"{BASE}/%2Fnotes%2Flog.md").mock(
        return_value=Response(404, json={"error": "not_found"})
    )

    result = await client.append_note("Notes/log.md", " more")
    assert result is True


@respx.mock
async def test_append_note_409_concurrent_modification(client):
    doc = _make_parent_doc("notes/log.md", ["h:lastchunk000"])
    # After 409, refetch returns doc with different last chunk (someone else modified)
    modified_doc = _make_parent_doc(
        "notes/log.md", ["h:lastchunk000", "h:newchunk0000"], _rev="2-mod"
    )

    # First GET returns original; second GET (refetch) returns modified
    respx.get(f"{BASE}/notes%2Flog.md").mock(
        side_effect=[
            Response(200, json=doc),
            Response(200, json=modified_doc),
        ]
    )
    respx.get(f"{BASE}/%2Fnotes%2Flog.md").mock(
        return_value=Response(404, json={"error": "not_found"})
    )
    _mock_all_docs({"h:lastchunk000": "existing"})

    # New chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # PUT returns 409
    respx.put(f"{BASE}/notes%2Flog.md").mock(return_value=Response(409, json={"error": "conflict"}))

    with pytest.raises(ValueError, match="modified concurrently"):
        await client.append_note("Notes/log.md", " more")


# ── delete_note ───────────────────────────────────────────────────


@respx.mock
async def test_delete_note(client):
    doc = _make_parent_doc("notes/old.md", ["h:chunk1aaaaaa"])
    _mock_get_doc("notes%2Fold.md", doc)
    # Reference scan: only this doc in the vault
    _mock_get_all_file_docs([doc])

    # Chunk GET for rev
    respx.get(f"{BASE}/h%3Achunk1aaaaaa").mock(
        return_value=Response(200, json={"_id": "h:chunk1aaaaaa", "_rev": "1-chk"})
    )
    # Chunk DELETE
    respx.delete(f"{BASE}/h%3Achunk1aaaaaa").mock(return_value=Response(200, json={"ok": True}))
    # Parent DELETE
    respx.delete(f"{BASE}/notes%2Fold.md").mock(return_value=Response(200, json={"ok": True}))

    result = await client.delete_note("Notes/old.md")
    assert result is True


@respx.mock
async def test_delete_note_not_found(client):
    _mock_get_doc_404("notes%2Fmissing.md")
    _mock_get_doc_404("%2Fnotes%2Fmissing.md")

    with pytest.raises(ValueError, match="Note not found"):
        await client.delete_note("Notes/missing.md")


@respx.mock
async def test_delete_note_409_conflict_retry(client):
    doc = _make_parent_doc("notes/old.md", ["h:chunk1aaaaaa"])
    _mock_get_doc("notes%2Fold.md", doc)
    # Reference scan: only this doc in the vault
    _mock_get_all_file_docs([doc])

    # Chunk GET + DELETE
    respx.get(f"{BASE}/h%3Achunk1aaaaaa").mock(
        return_value=Response(200, json={"_id": "h:chunk1aaaaaa", "_rev": "1-chk"})
    )
    respx.delete(f"{BASE}/h%3Achunk1aaaaaa").mock(return_value=Response(200, json={"ok": True}))
    # Parent DELETE returns 409 first time, then succeeds after refetch
    respx.delete(f"{BASE}/notes%2Fold.md").mock(
        side_effect=[
            Response(409, json={"error": "conflict"}),
            Response(200, json={"ok": True}),
        ]
    )
    # Refetch on conflict
    respx.get(f"{BASE}/%2Fnotes%2Fold.md").mock(
        return_value=Response(404, json={"error": "not_found"})
    )

    result = await client.delete_note("Notes/old.md")
    assert result is True


@respx.mock
async def test_delete_note_409_already_deleted(client):
    doc = _make_parent_doc("notes/old.md", [])

    # First GET returns doc; second GET (refetch after 409) returns 404
    respx.get(f"{BASE}/notes%2Fold.md").mock(
        side_effect=[
            Response(200, json=doc),
            Response(404, json={"error": "not_found"}),
        ]
    )
    respx.get(f"{BASE}/%2Fnotes%2Fold.md").mock(
        return_value=Response(404, json={"error": "not_found"})
    )

    # Parent DELETE returns 409
    respx.delete(f"{BASE}/notes%2Fold.md").mock(
        return_value=Response(409, json={"error": "conflict"})
    )

    result = await client.delete_note("Notes/old.md")
    assert result is True  # Success — note is gone, which is what we wanted


@respx.mock
async def test_delete_note_preserves_shared_chunk(client):
    """Deleting note A must not delete chunks still referenced by note B.

    Same dedup bug shape as the write_note orphan cleanup — the delete path
    unconditionally removes every chunk in the deleted note's manifest without
    checking whether other notes still reference those chunks.
    """
    shared_id = "h:shared000000"
    a_only_id = "h:aonly0000000"

    doc_a = _make_parent_doc("notes/a.md", [shared_id, a_only_id])
    note_b = _make_parent_doc("notes/b.md", [shared_id])

    _mock_get_doc("notes%2Fa.md", doc_a)
    # Reference check after fix: both A and B are seen so shared_id stays in-use
    _mock_get_all_file_docs([doc_a, note_b])

    # Mock chunk GET + DELETE for both chunks (so failures are visible)
    respx.get(f"{BASE}/{shared_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"_id": shared_id, "_rev": "1-s"})
    )
    shared_delete = respx.delete(f"{BASE}/{shared_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"ok": True})
    )
    respx.get(f"{BASE}/{a_only_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"_id": a_only_id, "_rev": "1-a"})
    )
    a_only_delete = respx.delete(f"{BASE}/{a_only_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"ok": True})
    )

    # Parent doc DELETE
    respx.delete(f"{BASE}/notes%2Fa.md").mock(return_value=Response(200, json={"ok": True}))

    result = await client.delete_note("Notes/a.md")
    assert result is True

    # Shared chunk must NOT be deleted — note B still references it
    assert not shared_delete.called, (
        "Shared chunk was deleted despite being referenced by another note — "
        "this is the dedup data-loss bug"
    )
    # a_only chunk should still be deleted (only A referenced it)
    assert a_only_delete.called, "Chunk exclusive to deleted note was not cleaned up"


# ── list_notes ────────────────────────────────────────────────────


@respx.mock
async def test_list_notes(client):
    docs = [
        _make_parent_doc("notes/a.md", ["h:c1"], path="Notes/a.md", mtime=2000),
        _make_parent_doc("notes/b.md", ["h:c2"], path="Notes/b.md", mtime=3000),
    ]
    _mock_get_all_file_docs(docs)

    results = await client.list_notes()
    assert len(results) == 2
    # Sorted by mtime descending
    assert results[0].path == "Notes/b.md"
    assert results[1].path == "Notes/a.md"


@respx.mock
async def test_list_notes_folder_filter(client):
    docs = [
        _make_parent_doc("notes/a.md", ["h:c1"], path="Notes/a.md"),
        _make_parent_doc("dev/b.md", ["h:c2"], path="Dev/b.md"),
    ]
    _mock_get_all_file_docs(docs)

    results = await client.list_notes(folder="Notes")
    assert len(results) == 1
    assert results[0].path == "Notes/a.md"


@respx.mock
async def test_list_notes_pagination(client):
    docs = [
        _make_parent_doc(f"notes/{i}.md", [f"h:c{i}"], path=f"Notes/{i}.md", mtime=i)
        for i in range(5)
    ]
    _mock_get_all_file_docs(docs)

    results = await client.list_notes(limit=2, skip=1)
    assert len(results) == 2


# ── search_notes ──────────────────────────────────────────────────


@respx.mock
async def test_search_notes(client):
    parent_doc = _make_parent_doc("notes/todo.md", ["h:searchchunk0"], path="Notes/todo.md")

    # _get_all_file_docs mock (two range queries)
    respx.get(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(200, json={"rows": [{"doc": parent_doc}]}),
            Response(200, json={"rows": []}),
        ]
    )

    # Mango search
    respx.post(f"{BASE}/_find").mock(
        return_value=Response(
            200, json={"docs": [{"_id": "h:searchchunk0", "data": "Buy milk and eggs"}]}
        )
    )

    results = await client.search_notes("milk")
    assert len(results) == 1
    assert results[0].path == "Notes/todo.md"
    assert results[0].matches == 1


@respx.mock
async def test_search_notes_no_results(client):
    respx.get(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(200, json={"rows": []}),
            Response(200, json={"rows": []}),
        ]
    )
    respx.post(f"{BASE}/_find").mock(return_value=Response(200, json={"docs": []}))

    results = await client.search_notes("nonexistent")
    assert results == []


# ── _read_note_content ────────────────────────────────────────────


@respx.mock
async def test_read_note_content_missing_chunk_returns_none(client):
    """_read_note_content logs warning and returns None on missing chunks."""
    doc = {"_id": "notes/broken.md", "children": ["h:missing00000"]}
    _mock_all_docs({})  # No chunks

    result = await client._read_note_content(doc)
    assert result is None


# ── list_folders ──────────────────────────────────────────────────


@respx.mock
async def test_list_folders(client):
    docs = [
        _make_parent_doc("notes/a.md", ["h:c1"], path="Notes/a.md"),
        _make_parent_doc("notes/b.md", ["h:c2"], path="Notes/b.md"),
        _make_parent_doc("dev/c.md", ["h:c3"], path="Dev/c.md"),
    ]
    respx.get(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(200, json={"rows": [{"doc": d} for d in docs]}),
            Response(200, json={"rows": []}),
        ]
    )

    folders = await client.list_folders()
    paths = [f.path for f in folders]
    assert "Dev" in paths
    assert "Notes" in paths


# ── frontmatter operations ────────────────────────────────────────


@respx.mock
async def test_read_frontmatter(client):
    content = "---\ntitle: Test\nstatus: draft\n---\nBody"
    doc = _make_parent_doc("notes/fm.md", ["h:fmchunk00000"])
    _mock_get_doc("notes%2Ffm.md", doc)
    _mock_all_docs({"h:fmchunk00000": content})

    fm = await client.read_frontmatter("Notes/fm.md")
    assert fm == {"title": "Test", "status": "draft"}


@respx.mock
async def test_read_frontmatter_none(client):
    doc = _make_parent_doc("notes/plain.md", ["h:plainchunk00"])
    _mock_get_doc("notes%2Fplain.md", doc)
    _mock_all_docs({"h:plainchunk00": "No frontmatter"})

    fm = await client.read_frontmatter("Notes/plain.md")
    assert fm is None


# ── update_frontmatter ────────────────────────────────────────────


@respx.mock
async def test_update_frontmatter_merge(client):
    content = "---\ntitle: Hello\n---\nBody"
    doc = _make_parent_doc("notes/fm.md", ["h:fmchunk00000"])
    _mock_get_doc("notes%2Ffm.md", doc)
    _mock_all_docs({"h:fmchunk00000": content})
    # Reference scan for orphan cleanup in write_note
    _mock_get_all_file_docs([doc])

    # write_note will: create chunk, then update parent
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    respx.put(f"{BASE}/notes%2Ffm.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-up"})
    )

    result = await client.update_frontmatter("Notes/fm.md", {"status": "done"})
    assert result is True


@respx.mock
async def test_update_frontmatter_not_found(client):
    _mock_get_doc_404("notes%2Fmissing.md")
    _mock_get_doc_404("%2Fnotes%2Fmissing.md")

    with pytest.raises(ValueError, match="Note not found"):
        await client.update_frontmatter("Notes/missing.md", {"k": "v"})


@respx.mock
async def test_update_frontmatter_binary_rejected(client):
    doc = _make_parent_doc("img/photo.png", ["h:binchunk0000"], type="newnote")
    _mock_get_doc("img%2Fphoto.png", doc)
    _mock_all_docs({"h:binchunk0000": "aGVsbG8="})

    with pytest.raises(ValueError, match="Cannot set frontmatter on binary"):
        await client.update_frontmatter("img/photo.png", {"k": "v"})


# ── list_tags ─────────────────────────────────────────────────────


@respx.mock
async def test_list_tags(client):
    doc1 = _make_parent_doc("notes/a.md", ["h:tagchunk0001"])
    doc2 = _make_parent_doc("notes/b.md", ["h:tagchunk0002"])
    _mock_get_all_file_docs([doc1, doc2])
    _mock_all_docs(
        {
            "h:tagchunk0001": "---\ntags: [project]\n---\n#urgent text",
            "h:tagchunk0002": "#project more text",
        }
    )

    tags = await client.list_tags()
    assert "project" in tags
    assert tags["project"] == 2
    assert "urgent" in tags


@respx.mock
async def test_list_tags_skips_binary(client):
    doc = _make_parent_doc("img/photo.png", ["h:binchunk0000"], type="newnote")
    _mock_get_all_file_docs([doc])

    tags = await client.list_tags()
    assert tags == {}


@respx.mock
async def test_list_tags_folder_filter(client):
    doc_in = _make_parent_doc("notes/a.md", ["h:tagchunk0001"])
    doc_out = _make_parent_doc("dev/b.md", ["h:tagchunk0002"])
    _mock_get_all_file_docs([doc_in, doc_out])
    _mock_all_docs({"h:tagchunk0001": "#intag"})

    tags = await client.list_tags(folder="Notes")
    assert "intag" in tags


# ── search_by_tag ─────────────────────────────────────────────────


@respx.mock
async def test_search_by_tag(client):
    doc = _make_parent_doc("notes/a.md", ["h:tagchunk0001"])
    _mock_get_all_file_docs([doc])
    _mock_all_docs({"h:tagchunk0001": "#project some text"})

    results = await client.search_by_tag("project")
    assert len(results) == 1
    assert results[0].path == "notes/a.md"


@respx.mock
async def test_search_by_tag_case_insensitive(client):
    doc = _make_parent_doc("notes/a.md", ["h:tagchunk0001"])
    _mock_get_all_file_docs([doc])
    _mock_all_docs({"h:tagchunk0001": "#Project text"})

    results = await client.search_by_tag("#project")
    assert len(results) == 1


@respx.mock
async def test_search_by_tag_no_match(client):
    doc = _make_parent_doc("notes/a.md", ["h:tagchunk0001"])
    _mock_get_all_file_docs([doc])
    _mock_all_docs({"h:tagchunk0001": "#other text"})

    results = await client.search_by_tag("missing")
    assert results == []


# ── get_outbound_links ────────────────────────────────────────────


@respx.mock
async def test_get_outbound_links(client):
    content = "See [[Todo]] and [[Projects/Readme]]"
    doc = _make_parent_doc("notes/a.md", ["h:linkchunk000"])
    _mock_get_doc("notes%2Fa.md", doc)
    _mock_all_docs({"h:linkchunk000": content})

    links = await client.get_outbound_links("Notes/a.md")
    assert "Todo" in links
    assert "Projects/Readme" in links


@respx.mock
async def test_get_outbound_links_empty(client):
    doc = _make_parent_doc("notes/a.md", ["h:linkchunk000"])
    _mock_get_doc("notes%2Fa.md", doc)
    _mock_all_docs({"h:linkchunk000": "No links here"})

    links = await client.get_outbound_links("Notes/a.md")
    assert links == []


@respx.mock
async def test_get_outbound_links_binary_returns_empty(client):
    doc = _make_parent_doc("img/x.png", ["h:binchunk0000"], type="newnote")
    _mock_get_doc("img%2Fx.png", doc)
    _mock_all_docs({"h:binchunk0000": "data"})

    links = await client.get_outbound_links("img/x.png")
    assert links == []


# ── get_backlinks ─────────────────────────────────────────────────


@respx.mock
async def test_get_backlinks(client):
    source_doc = _make_parent_doc("notes/source.md", ["h:blchunk00000"], path="Notes/source.md")
    _mock_get_all_file_docs([source_doc])
    _mock_all_docs({"h:blchunk00000": "Check out [[Todo]] for tasks"})

    backlinks = await client.get_backlinks("Notes/Todo.md")
    assert len(backlinks) == 1
    assert backlinks[0].source_path == "Notes/source.md"
    assert "[[Todo]]" in backlinks[0].context


@respx.mock
async def test_get_backlinks_no_match(client):
    source_doc = _make_parent_doc("notes/source.md", ["h:blchunk00000"], path="Notes/source.md")
    _mock_get_all_file_docs([source_doc])
    _mock_all_docs({"h:blchunk00000": "No links here"})

    backlinks = await client.get_backlinks("Notes/Todo.md")
    assert backlinks == []


@respx.mock
async def test_get_backlinks_skips_binary(client):
    binary_doc = _make_parent_doc(
        "img/photo.png", ["h:binchunk0000"], path="img/photo.png", type="newnote"
    )
    _mock_get_all_file_docs([binary_doc])

    backlinks = await client.get_backlinks("Notes/Todo.md")
    assert backlinks == []


# ── legacy notes type ────────────────────────────────────────────


@respx.mock
async def test_read_note_legacy_notes_type_string(client):
    """Legacy 'notes' type stores content directly in data field as a string."""
    doc = {
        "_id": "notes/old.md",
        "_rev": "1-abc",
        "data": "Legacy content here",
        "type": "notes",
        "ctime": 1700000000000,
        "mtime": 1700000000000,
        "size": 19,
        "path": "Notes/old.md",
    }
    _mock_get_doc("notes%2Fold.md", doc)

    result = await client.read_note("Notes/old.md")
    assert result is not None
    assert result.content == "Legacy content here"
    assert result.is_binary is False


@respx.mock
async def test_read_note_legacy_notes_type_list(client):
    """Legacy 'notes' type can store content as a list of strings."""
    doc = {
        "_id": "notes/old.md",
        "_rev": "1-abc",
        "data": ["Part one. ", "Part two."],
        "type": "notes",
        "ctime": 1700000000000,
        "mtime": 1700000000000,
        "size": 19,
        "path": "Notes/old.md",
    }
    _mock_get_doc("notes%2Fold.md", doc)

    result = await client.read_note("Notes/old.md")
    assert result is not None
    assert result.content == "Part one. Part two."


@respx.mock
async def test_list_notes_includes_legacy_type(client):
    """list_notes should include legacy 'notes' type documents."""
    docs = [
        _make_parent_doc("notes/a.md", ["h:c1"], path="Notes/a.md"),
        {
            "_id": "notes/old.md",
            "_rev": "1-abc",
            "data": "old content",
            "type": "notes",
            "ctime": 1700000000000,
            "mtime": 1700000000000,
            "size": 11,
            "path": "Notes/old.md",
        },
    ]
    _mock_get_all_file_docs(docs)

    results = await client.list_notes()
    assert len(results) == 2


# ── orphan chunk cleanup ─────────────────────────────────────────


@respx.mock
async def test_write_note_cleans_up_orphan_chunks(client):
    """Updating a note should delete old chunks no longer referenced."""
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])
    _mock_get_doc("notes%2Ftodo.md", existing)
    # Reference scan: only this doc, so the old chunk is truly orphaned
    _mock_get_all_file_docs([existing])

    # New chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # Parent doc update
    respx.put(f"{BASE}/notes%2Ftodo.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-updated"})
    )
    # Old chunk GET for rev (needed for delete)
    respx.get(f"{BASE}/h%3Aoldchunk0000").mock(
        return_value=Response(200, json={"_id": "h:oldchunk0000", "_rev": "1-old"})
    )
    # Old chunk DELETE
    delete_route = respx.delete(f"{BASE}/h%3Aoldchunk0000").mock(
        return_value=Response(200, json={"ok": True})
    )

    result = await client.write_note("Notes/todo.md", "Updated content")
    assert result is True
    assert delete_route.called


@respx.mock
async def test_write_note_orphan_cleanup_failure_nonfatal(client):
    """Failed chunk cleanup should log warning, not fail the write."""
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])
    _mock_get_doc("notes%2Ftodo.md", existing)
    # Reference scan: only this doc, so the old chunk is considered orphaned
    _mock_get_all_file_docs([existing])

    # New chunk creation
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # Parent doc update
    respx.put(f"{BASE}/notes%2Ftodo.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-updated"})
    )
    # Old chunk GET returns 500 (cleanup fails)
    respx.get(f"{BASE}/h%3Aoldchunk0000").mock(
        return_value=Response(500, json={"error": "internal"})
    )

    # Write should still succeed
    result = await client.write_note("Notes/todo.md", "Updated content")
    assert result is True
