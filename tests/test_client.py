"""Tests for obsidian_livesync_mcp.client — async CouchDB client with respx mocking."""

import pytest
import respx
from httpx import Response

from obsidian_livesync_mcp.chunking import split_chunks
from obsidian_livesync_mcp.client import ObsidianVaultClient
from obsidian_livesync_mcp.config import Config
from obsidian_livesync_mcp.utils import encode_doc_id, generate_chunk_id

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
        await client.read_note("Notes/todo.md", retry_delay=0.0)


@respx.mock
async def test_read_note_retries_transient_missing_chunk(client):
    """A stale parent is re-fetched so a concurrent rewrite's chunks resolve."""
    stale_doc = _make_parent_doc("notes/hot.md", ["h:old"], size=5)
    fresh_doc = _make_parent_doc("notes/hot.md", ["h:new"], size=5)
    respx.get(f"{BASE}/notes%2Fhot.md").mock(
        side_effect=[
            Response(200, json=stale_doc),
            Response(200, json=fresh_doc),
        ]
    )
    # First fetch: the stale parent's chunk is gone. Second: the fresh parent's
    # chunk has arrived.
    respx.post(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(200, json={"rows": []}),
            Response(200, json={"rows": [{"id": "h:new", "doc": {"data": "Hello"}}]}),
        ]
    )

    result = await client.read_note("Notes/hot.md", retry_delay=0.0)
    assert result is not None
    assert result.content == "Hello"


@respx.mock
async def test_read_note_raises_after_persistent_missing_chunk(client):
    """A chunk that never resolves still errors once retries are exhausted."""
    doc = _make_parent_doc("notes/broken.md", ["h:gone"])
    _mock_get_doc("notes%2Fbroken.md", doc)
    respx.post(f"{BASE}/_all_docs").mock(return_value=Response(200, json={"rows": []}))

    with pytest.raises(ValueError, match="Missing 1 chunk"):
        await client.read_note("Notes/broken.md", retries=2, retry_delay=0.0)


@respx.mock
async def test_read_note_binary(client):
    doc = _make_parent_doc("img/photo.png", ["h:binchunk0000"], type="newnote")
    _mock_get_doc("img%2Fphoto.png", doc)
    _mock_all_docs({"h:binchunk0000": "aGVsbG8="})

    result = await client.read_note("img/photo.png")
    assert result.is_binary is True
    assert result.content == "aGVsbG8="


@respx.mock
async def test_read_note_binary_roundtrips_multichunk(client):
    """Binary read returns base64 for the whole file, not concatenated chunk base64."""
    import base64 as _b64

    raw = bytes((i * 53) & 0xFF for i in range(20))
    part_a, part_b = raw[:7], raw[7:]
    chunk_a = _b64.b64encode(part_a).decode("ascii")
    chunk_b = _b64.b64encode(part_b).decode("ascii")

    doc = _make_parent_doc("img/photo.png", ["h:bin_a", "h:bin_b"], type="newnote", size=len(raw))
    _mock_get_doc("img%2Fphoto.png", doc)
    _mock_all_docs({"h:bin_a": chunk_a, "h:bin_b": chunk_b})

    result = await client.read_note("img/photo.png")
    assert result is not None
    assert result.is_binary is True
    assert _b64.b64decode(result.content) == raw
    assert result.size == len(raw)


@respx.mock
async def test_read_note_binary_retries_transient_missing_chunk(client):
    import base64 as _b64

    stale_doc = _make_parent_doc("img/photo.png", ["h:old"], type="newnote", size=3)
    fresh_doc = _make_parent_doc("img/photo.png", ["h:new"], type="newnote", size=3)
    respx.get(f"{BASE}/img%2Fphoto.png").mock(
        side_effect=[
            Response(200, json=stale_doc),
            Response(200, json=fresh_doc),
        ]
    )
    respx.post(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(200, json={"rows": []}),
            Response(
                200,
                json={
                    "rows": [
                        {
                            "id": "h:new",
                            "doc": {"data": _b64.b64encode(b"new").decode("ascii")},
                        }
                    ]
                },
            ),
        ]
    )

    result = await client.read_note("img/photo.png", retry_delay=0.0)
    assert result is not None
    assert _b64.b64decode(result.content) == b"new"


@respx.mock
async def test_read_attachment_retries_transient_missing_chunk(client):
    import base64 as _b64

    stale_doc = _make_parent_doc("img/photo.png", ["h:old"], type="newnote", size=3)
    fresh_doc = _make_parent_doc("img/photo.png", ["h:new"], type="newnote", size=3)
    respx.get(f"{BASE}/img%2Fphoto.png").mock(
        side_effect=[
            Response(200, json=stale_doc),
            Response(200, json=fresh_doc),
        ]
    )
    respx.post(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(200, json={"rows": []}),
            Response(
                200,
                json={
                    "rows": [
                        {
                            "id": "h:new",
                            "doc": {"data": _b64.b64encode(b"new").decode("ascii")},
                        }
                    ]
                },
            ),
        ]
    )

    result = await client.read_attachment("img/photo.png")
    assert result is not None
    assert result.data == b"new"


@respx.mock
async def test_reassemble_binary_raises_on_missing_chunk(client):
    doc = _make_parent_doc("img/x.png", ["h:present", "h:gone"], type="newnote")
    _mock_all_docs({"h:present": "AAA="})

    with pytest.raises(ValueError, match="Missing 1 chunk"):
        await client._reassemble_binary(doc)


def test_attachment_content_to_dict_base64():
    from obsidian_livesync_mcp.models import AttachmentContent

    att = AttachmentContent(path="img/x.png", data=b"hello", size=5, content_type="image/png")
    d = att.to_dict()
    assert d["data_base64"] == "aGVsbG8="
    assert d["content_type"] == "image/png"
    assert d["size"] == 5
    assert d["path"] == "img/x.png"


def test_attachment_metadata_to_dict():
    from obsidian_livesync_mcp.models import AttachmentMetadata

    meta = AttachmentMetadata(
        path="img/x.png", size=10, ctime=1, mtime=2, extension="png", chunk_count=3
    )
    d = meta.to_dict()
    assert d["extension"] == "png"
    assert d["chunks"] == 3


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
async def test_write_does_not_delete_orphan_chunks(client):
    """Updating a note must NOT issue DELETE on chunks dropped from the parent.

    Auto-deletion tombstones content-addressed chunks, which can break other
    notes and is the suspected cause of the 2026-06-15 tombstone incident.
    """
    doc_id = "context/note.md"
    existing = _make_parent_doc(doc_id, ["h:oldchunk1", "h:oldchunk2"], _rev="5-old")

    _mock_get_doc(encode_doc_id(doc_id), existing)
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(return_value=Response(201, json={"ok": True}))
    respx.put(f"{BASE}/{encode_doc_id(doc_id)}").mock(return_value=Response(201, json={"ok": True}))

    delete_route = respx.delete(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(200, json={"ok": True})
    )

    await client.write_note("Context/note.md", "completely new content body")

    assert not delete_route.called, "write_note must not DELETE any chunk doc"


@respx.mock
async def test_write_note_resurrects_tombstoned_chunk_conflict(client):
    """A chunk 409 can mean a deleted CouchDB tombstone, not live chunk data."""
    import json as _json

    content = "Recovered content"
    chunk_id = generate_chunk_id(content)
    encoded_chunk_id = encode_doc_id(chunk_id)

    _mock_get_doc_404("notes%2Fnew.md")
    _mock_get_doc_404("%2Fnotes%2Fnew.md")

    chunk_put = respx.put(f"{BASE}/{encoded_chunk_id}").mock(
        side_effect=[
            Response(409, json={"error": "conflict", "reason": "Document update conflict."}),
            Response(201, json={"ok": True, "id": chunk_id, "rev": "3-live"}),
        ]
    )
    respx.post(f"{BASE}/_all_docs").mock(
        return_value=Response(
            200,
            json={
                "rows": [
                    {
                        "id": chunk_id,
                        "key": chunk_id,
                        "value": {"rev": "2-deleted", "deleted": True},
                        "doc": None,
                    }
                ]
            },
        )
    )
    parent_put = respx.put(f"{BASE}/notes%2Fnew.md").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-doc"})
    )

    result = await client.write_note("Notes/new.md", content)

    assert result is True
    assert len(chunk_put.calls) == 2
    resurrect_body = _json.loads(chunk_put.calls[1].request.content)
    assert resurrect_body == {
        "_id": chunk_id,
        "_rev": "2-deleted",
        "data": content,
        "type": "leaf",
    }
    parent_body = _json.loads(parent_put.calls[0].request.content)
    assert parent_body["children"] == [chunk_id]


@respx.mock
async def test_write_resurrects_tombstoned_chunk(client):
    """A needed tombstoned chunk must be PUT with its reusable tombstone rev."""
    import json as _json

    content = "x"
    chunk_id = generate_chunk_id(content)
    chunk_route = respx.put(f"{BASE}/{encode_doc_id(chunk_id)}").mock(
        side_effect=[
            Response(409, json={"error": "conflict"}),
            Response(201, json={"ok": True, "rev": "3-live"}),
        ]
    )
    tombstone_lookup = respx.post(f"{BASE}/_all_docs").mock(
        return_value=Response(
            200,
            json={
                "rows": [
                    {
                        "id": chunk_id,
                        "value": {"rev": "2-dead", "deleted": True},
                        "doc": {"_id": chunk_id, "_rev": "2-dead", "_deleted": True},
                    }
                ]
            },
        )
    )
    _mock_get_doc_404(encode_doc_id("context/new.md"))
    _mock_get_doc_404(encode_doc_id("/context/new.md"))
    respx.put(f"{BASE}/{encode_doc_id('context/new.md')}").mock(
        return_value=Response(201, json={"ok": True})
    )

    await client.write_note("Context/new.md", content)

    lookup_body = _json.loads(tombstone_lookup.calls[0].request.content)
    assert lookup_body["keys"] == [chunk_id]
    resurrect_body = _json.loads(chunk_route.calls[-1].request.content)
    assert resurrect_body["_rev"] == "2-dead"


@respx.mock
async def test_parent_put_happens_after_chunk_puts(client):
    """The parent doc must be PUT only after all chunk PUTs succeed."""
    order = []
    content = "some content body here\n" * 200
    expected_chunks = split_chunks(content.encode("utf-8"), is_text=True)
    assert len(expected_chunks) > 1
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        side_effect=lambda request: order.append("chunk") or Response(201, json={"ok": True})
    )
    parent_id = encode_doc_id("context/ordered.md")
    _mock_get_doc_404(parent_id)
    _mock_get_doc_404(encode_doc_id("/context/ordered.md"))
    respx.put(f"{BASE}/{parent_id}").mock(
        side_effect=lambda request: order.append("parent") or Response(201, json={"ok": True})
    )

    await client.write_note("Context/ordered.md", content)

    assert order[-1] == "parent", "parent must be written last"
    assert order == ["chunk"] * len(expected_chunks) + ["parent"]


@respx.mock
async def test_write_note_409_conflict_retry(client):
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])

    # First GET returns existing doc
    _mock_get_doc("notes%2Ftodo.md", existing)

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
    """Updating note A must not delete dropped chunks, shared or unshared.

    Chunks are content-addressed and CouchDB tombstones are sticky at the
    replication layer. Write-time chunk deletion is unsafe even when a chunk
    appears to be unreferenced.
    """
    shared_id = "h:shared000000"
    a_only_id = "h:aonly0000000"

    old_a = _make_parent_doc("notes/a.md", [shared_id, a_only_id])

    # write_note will GET the existing doc A
    _mock_get_doc("notes%2Fa.md", old_a)

    # New chunks for updated A content
    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    # Parent doc A update
    respx.put(f"{BASE}/notes%2Fa.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-updated"})
    )

    # Routes that would catch any write-path chunk deletion.
    shared_delete = respx.delete(f"{BASE}/{shared_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"ok": True})
    )
    a_only_delete = respx.delete(f"{BASE}/{a_only_id.replace(':', '%3A')}").mock(
        return_value=Response(200, json={"ok": True})
    )

    result = await client.write_note("Notes/a.md", "completely different new content")
    assert result is True

    assert not shared_delete.called, (
        "Shared chunk was deleted despite being referenced by another note — "
        "this is the dedup data-loss bug"
    )
    assert not a_only_delete.called, "write_note must not delete dropped chunk docs"


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


# ── delete_note (soft-delete default, livesync-compatible) ───────


@respx.mock
async def test_delete_note_default_is_soft(client):
    """Default delete_note should soft-delete: PUT the doc with `deleted=True`
    and a bumped `mtime`, preserving chunks. This matches obsidian-livesync's
    own delete flow (deleteDBEntryByPath in EntryManagerImpls.ts) and is the
    only form livesync's apply-to-storage path cleans up properly on devices.
    """
    import json as _json

    original_mtime = 1700000000000
    doc = _make_parent_doc("notes/old.md", ["h:chunk1aaaaaa"], mtime=original_mtime)
    _mock_get_doc("notes%2Fold.md", doc)

    # Soft-delete does a PUT on the parent doc — not a CouchDB DELETE.
    put_route = respx.put(f"{BASE}/notes%2Fold.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-softdel"})
    )

    result = await client.delete_note("Notes/old.md")
    assert result is True

    # PUT must have been called with deleted=True and a bumped mtime
    assert put_route.called, "Soft-delete should PUT the parent doc"
    body = _json.loads(put_route.calls[0].request.content)
    assert body.get("deleted") is True, f"Expected deleted=True in body, got: {body}"
    assert body.get("mtime", 0) > original_mtime, (
        f"Expected mtime to be bumped past {original_mtime}, got {body.get('mtime')}"
    )
    # Chunks must be preserved — livesync needs them for conflict resolution
    assert body.get("children") == ["h:chunk1aaaaaa"], (
        f"Expected children chunks preserved in soft-delete, got: {body.get('children')}"
    )


@respx.mock
async def test_delete_note_soft_preserves_chunks(client):
    """Soft-delete must NOT touch chunks, even chunks that are unique to the
    deleted note. Livesync's replication may revive a soft-deleted doc (CRDT-
    style conflict resolution) and will need those chunks intact to rehydrate
    the content on another device.
    """
    doc = _make_parent_doc("notes/a.md", ["h:only0aaaaaaa"])
    _mock_get_doc("notes%2Fa.md", doc)

    # Mock chunk endpoints so we can assert they're NOT called. (Without mocks,
    # respx would raise on any touch — also acceptable — but explicit `called`
    # assertions make the test's intent obvious.)
    chunk_get = respx.get(f"{BASE}/h%3Aonly0aaaaaaa").mock(
        return_value=Response(200, json={"_id": "h:only0aaaaaaa", "_rev": "1-c"})
    )
    chunk_delete = respx.delete(f"{BASE}/h%3Aonly0aaaaaaa").mock(
        return_value=Response(200, json={"ok": True})
    )
    respx.put(f"{BASE}/notes%2Fa.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-softdel"})
    )

    result = await client.delete_note("Notes/a.md")
    assert result is True
    assert not chunk_get.called, "Soft-delete should not touch chunk docs"
    assert not chunk_delete.called, "Soft-delete must NOT delete chunks"


@respx.mock
async def test_delete_note_soft_409_retry(client):
    """On PUT 409 during soft-delete, refetch and retry once using the fresh
    `_rev`. Mirrors the write_note conflict-resolution pattern at client.py:315.
    """
    import json as _json

    doc_v1 = _make_parent_doc("notes/old.md", ["h:chunk1aaaaaa"], _rev="1-abc", mtime=1700000000000)
    doc_v2 = _make_parent_doc("notes/old.md", ["h:chunk1aaaaaa"], _rev="2-xyz", mtime=1700000000001)

    # First GET returns v1; refetch (after 409) returns v2
    respx.get(f"{BASE}/notes%2Fold.md").mock(
        side_effect=[
            Response(200, json=doc_v1),
            Response(200, json=doc_v2),
        ]
    )
    respx.get(f"{BASE}/%2Fnotes%2Fold.md").mock(
        return_value=Response(404, json={"error": "not_found"})
    )

    put_route = respx.put(f"{BASE}/notes%2Fold.md").mock(
        side_effect=[
            Response(409, json={"error": "conflict"}),
            Response(200, json={"ok": True, "rev": "3-resolved"}),
        ]
    )

    result = await client.delete_note("Notes/old.md")
    assert result is True
    assert len(put_route.calls) == 2, (
        f"Expected 2 PUT calls (original + retry), got {len(put_route.calls)}"
    )
    # Retry PUT must use the fresh _rev and still carry the soft-delete flag
    retry_body = _json.loads(put_route.calls[1].request.content)
    assert retry_body.get("_rev") == "2-xyz", (
        f"Retry should use refreshed _rev, got: {retry_body.get('_rev')}"
    )
    assert retry_body.get("deleted") is True


@respx.mock
async def test_delete_note_soft_409_already_deleted(client):
    """If the refetch during soft-delete 409 retry returns nothing, the note is
    already gone (race with another client). Idempotent success.
    """
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

    # PUT returns 409 both times (second would never fire since refetch is None)
    respx.put(f"{BASE}/notes%2Fold.md").mock(return_value=Response(409, json={"error": "conflict"}))

    result = await client.delete_note("Notes/old.md")
    assert result is True  # already gone -> success


# ── delete_note (hard-delete opt-in, for broken-manifest cleanup) ────


@respx.mock
async def test_delete_note_hard_opt_in(client):
    """delete_note(path, hard=True) keeps the pre-fix behavior: CouchDB DELETE
    on the parent doc plus orphan chunk cleanup. Still needed for broken-
    manifest cleanup (Rule 3 in the MCP/livesync memory file).
    """
    doc = _make_parent_doc("notes/old.md", ["h:chunk1aaaaaa"])
    _mock_get_doc("notes%2Fold.md", doc)
    _mock_get_all_file_docs([doc])

    respx.get(f"{BASE}/h%3Achunk1aaaaaa").mock(
        return_value=Response(200, json={"_id": "h:chunk1aaaaaa", "_rev": "1-chk"})
    )
    chunk_delete = respx.delete(f"{BASE}/h%3Achunk1aaaaaa").mock(
        return_value=Response(200, json={"ok": True})
    )
    parent_delete = respx.delete(f"{BASE}/notes%2Fold.md").mock(
        return_value=Response(200, json={"ok": True})
    )

    result = await client.delete_note("Notes/old.md", hard=True)
    assert result is True
    assert chunk_delete.called, "Hard-delete should clean up chunks"
    assert parent_delete.called, "Hard-delete should DELETE the parent doc"


# ── delete_note (legacy hard-delete regression tests) ─────────────


@respx.mock
async def test_delete_note_hard(client):
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

    result = await client.delete_note("Notes/old.md", hard=True)
    assert result is True


@respx.mock
async def test_delete_note_not_found(client):
    _mock_get_doc_404("notes%2Fmissing.md")
    _mock_get_doc_404("%2Fnotes%2Fmissing.md")

    with pytest.raises(ValueError, match="Note not found"):
        await client.delete_note("Notes/missing.md")


@respx.mock
async def test_delete_note_hard_409_conflict_retry(client):
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

    result = await client.delete_note("Notes/old.md", hard=True)
    assert result is True


@respx.mock
async def test_delete_note_hard_409_already_deleted(client):
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

    result = await client.delete_note("Notes/old.md", hard=True)
    assert result is True  # Success — note is gone, which is what we wanted


@respx.mock
async def test_delete_note_hard_preserves_shared_chunk(client):
    """Hard-deleting note A must not delete chunks still referenced by note B.

    Hard-delete is the remaining write path that can remove chunk docs. It must
    check whether other notes still reference a chunk before deleting it.
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

    result = await client.delete_note("Notes/a.md", hard=True)
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
async def test_write_note_leaves_orphan_chunks_for_explicit_prune(client):
    """Updating a note leaves dropped chunks for explicit maintenance pruning."""
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])
    _mock_get_doc("notes%2Ftodo.md", existing)

    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    respx.put(f"{BASE}/notes%2Ftodo.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-updated"})
    )
    respx.get(f"{BASE}/h%3Aoldchunk0000").mock(
        return_value=Response(200, json={"_id": "h:oldchunk0000", "_rev": "1-old"})
    )
    delete_route = respx.delete(f"{BASE}/h%3Aoldchunk0000").mock(
        return_value=Response(200, json={"ok": True})
    )

    result = await client.write_note("Notes/todo.md", "Updated content")
    assert result is True
    assert not delete_route.called


@respx.mock
async def test_write_note_does_not_probe_dropped_chunks_for_cleanup(client):
    """Dropped chunk cleanup is no longer attempted from the write path."""
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])
    _mock_get_doc("notes%2Ftodo.md", existing)

    respx.put(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(201, json={"ok": True, "rev": "1-new"})
    )
    respx.put(f"{BASE}/notes%2Ftodo.md").mock(
        return_value=Response(200, json={"ok": True, "rev": "2-updated"})
    )
    old_chunk_get = respx.get(f"{BASE}/h%3Aoldchunk0000").mock(
        return_value=Response(500, json={"error": "internal"})
    )

    result = await client.write_note("Notes/todo.md", "Updated content")
    assert result is True
    assert not old_chunk_get.called


@respx.mock
async def test_prune_orphan_chunks_dry_run_lists_but_does_not_delete(client):
    parent = _make_parent_doc("notes/a.md", ["h:used"])
    respx.get(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(200, json={"rows": [{"id": "h:used"}, {"id": "h:orphan"}]}),
            Response(200, json={"rows": [{"doc": parent}]}),
            Response(200, json={"rows": []}),
        ]
    )
    delete_route = respx.delete(url__regex=rf"{BASE}/h%3A.*").mock(
        return_value=Response(200, json={"ok": True})
    )

    report = await client.prune_orphan_chunks(dry_run=True)

    assert report.total_chunks == 2
    assert report.referenced == 1
    assert report.orphan_chunk_ids == ["h:orphan"]
    assert report.deleted == 0
    assert not delete_route.called


@respx.mock
async def test_prune_orphan_chunks_counts_successful_deletes(client):
    parent = _make_parent_doc("notes/a.md", ["h:used"])
    respx.get(f"{BASE}/_all_docs").mock(
        side_effect=[
            Response(
                200,
                json={
                    "rows": [
                        {"id": "h:used"},
                        {"id": "h:delete"},
                        {"id": "h:fail"},
                        {"id": "h:missing"},
                    ]
                },
            ),
            Response(200, json={"rows": [{"doc": parent}]}),
            Response(200, json={"rows": []}),
        ]
    )
    respx.get(f"{BASE}/h%3Adelete").mock(
        return_value=Response(200, json={"_id": "h:delete", "_rev": "1-delete"})
    )
    respx.get(f"{BASE}/h%3Afail").mock(
        return_value=Response(200, json={"_id": "h:fail", "_rev": "1-fail"})
    )
    respx.get(f"{BASE}/h%3Amissing").mock(return_value=Response(404, json={"error": "not_found"}))
    delete_route = respx.delete(f"{BASE}/h%3Adelete").mock(
        return_value=Response(200, json={"ok": True})
    )
    fail_route = respx.delete(f"{BASE}/h%3Afail").mock(
        return_value=Response(500, json={"error": "server_error"})
    )

    report = await client.prune_orphan_chunks(dry_run=False)

    assert report.orphan_chunk_ids == ["h:delete", "h:fail", "h:missing"]
    assert report.deleted == 1
    assert delete_route.called
    assert fail_route.called


# ── soft-delete filtering ────────────────────────────────────────


@respx.mock
async def test_list_notes_excludes_soft_deleted(client):
    """Notes with LiveSync's ``deleted: True`` flag are hidden from list_notes."""
    docs = [
        _make_parent_doc("notes/alive.md", ["h:c1"], path="Notes/alive.md", mtime=3000),
        _make_parent_doc("notes/dead.md", ["h:c2"], path="Notes/dead.md", mtime=2000, deleted=True),
    ]
    _mock_get_all_file_docs(docs)

    results = await client.list_notes()
    assert len(results) == 1
    assert results[0].path == "Notes/alive.md"


@respx.mock
async def test_list_folders_excludes_soft_deleted(client):
    """Soft-deleted notes don't contribute to folder counts."""
    docs = [
        _make_parent_doc("notes/alive.md", ["h:c1"], path="Notes/alive.md"),
        _make_parent_doc("notes/dead.md", ["h:c2"], path="Notes/dead.md", deleted=True),
        _make_parent_doc("dev/ok.md", ["h:c3"], path="Dev/ok.md"),
    ]
    _mock_get_all_file_docs(docs)

    folders = await client.list_folders()
    folder_map = {f.path: f.note_count for f in folders}
    assert folder_map.get("Notes") == 1
    assert folder_map.get("Dev") == 1


@respx.mock
async def test_get_all_file_docs_include_deleted(client):
    """include_deleted=True returns soft-deleted docs (for chunk bookkeeping)."""
    docs = [
        _make_parent_doc("notes/alive.md", ["h:c1"]),
        _make_parent_doc("notes/dead.md", ["h:c2"], deleted=True),
    ]
    _mock_get_all_file_docs(docs)

    results = await client._get_all_file_docs(include_deleted=True)
    assert len(results) == 2


@respx.mock
async def test_read_note_returns_none_for_soft_deleted(client):
    """read_note treats soft-deleted docs as not found."""
    doc = _make_parent_doc("notes/dead.md", ["h:c1"], path="Notes/dead.md", deleted=True)
    _mock_get_doc("notes%2Fdead.md", doc)

    result = await client.read_note("Notes/dead.md")
    assert result is None
