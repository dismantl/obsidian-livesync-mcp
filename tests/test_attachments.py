"""Tests for attachment operations."""

import base64

import pytest
import respx
from httpx import Response

from obsidian_livesync_mcp.attachments import AttachmentOps
from obsidian_livesync_mcp.client import ObsidianVaultClient
from obsidian_livesync_mcp.config import Config
from obsidian_livesync_mcp.models import NoteContent

BASE = "http://test:5984/test-vault"


class _Response:
    def __init__(self, body=None, status_code=201):
        self._body = body or {}
        self.status_code = status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        return None


class _PutClient:
    def __init__(self, owner):
        self.owner = owner

    async def put(self, _url, json):
        path = json["path"].lstrip("/")
        stored = dict(json)
        stored["_rev"] = _next_rev(json.get("_rev"))
        self.owner.docs[path.lower()] = stored
        self.owner.put_docs.append(stored)
        if self.owner.after_put:
            self.owner.after_put(stored)
        return _Response({"ok": True, "id": stored["_id"], "rev": stored["_rev"]})


class _MemoryAttachmentClient(AttachmentOps):
    def __init__(self, docs=None, raw=None):
        self.docs = {doc["path"].lower(): dict(doc) for doc in docs or []}
        self.raw = raw or {}
        self.deleted = []
        self.deleted_chunks = []
        self.fail_writes = set()
        self.writes = []
        self.put_docs = []
        self.after_put = None
        self.iter_file_docs_calls = 0
        self.get_all_file_docs_calls = 0

    async def _get_client(self):
        return _PutClient(self)

    def _doc_id(self, vault_path: str) -> str:
        return vault_path.lstrip("/").lower()

    async def _get_doc(self, path: str):
        doc = self.docs.get(path.lstrip("/").lower())
        return dict(doc) if doc else None

    async def _get_all_file_docs(self, include_deleted: bool = False):
        self.get_all_file_docs_calls += 1
        return [doc for doc in self.docs.values() if include_deleted or not doc.get("deleted")]

    async def _iter_file_docs(
        self,
        *,
        include_deleted: bool = False,
        folder: str | None = None,
        batch_size: int = 100,
    ):
        self.iter_file_docs_calls += 1
        folder_lower = folder.strip("/").lower() + "/" if folder else None
        for doc in self.docs.values():
            if not include_deleted and doc.get("deleted"):
                continue
            if folder_lower and not doc.get("path", doc.get("_id", "")).lower().startswith(
                folder_lower
            ):
                continue
            yield doc

    async def _read_note_content(self, doc):
        return doc.get("content")

    async def _reassemble_binary(self, doc, chunks=None):
        return self.raw.get(doc["path"], b"")

    async def _collect_chunks_in_use_by_other_docs(self, exclude_doc_id: str):
        in_use = set()
        for doc in self.docs.values():
            if doc.get("_id") == exclude_doc_id:
                continue
            in_use.update(doc.get("children", []))
        return in_use

    async def _delete_orphan_chunks(self, chunk_ids):
        self.deleted_chunks.extend(chunk_ids)

    async def read_note(self, path: str):
        doc = await self._get_doc(path)
        if not doc or doc.get("deleted"):
            return None
        if doc.get("type") == "newnote":
            raw = await self._reassemble_binary(doc)
            return NoteContent(
                path=doc.get("path", path),
                content=base64.b64encode(raw).decode("ascii"),
                size=len(raw),
                is_binary=True,
            )
        content = doc.get("content", "")
        return NoteContent(
            path=doc.get("path", path),
            content=content,
            size=doc.get("size", len(content.encode("utf-8"))),
            is_binary=False,
        )

    async def _write_file_doc(
        self, path: str, raw: bytes, is_text: bool, expected_rev: str | None = None
    ):
        vault_path = path.lstrip("/")
        if vault_path in self.fail_writes:
            raise ValueError(f"write failed: {vault_path}")
        existing = self.docs.get(vault_path.lower())
        if expected_rev and (not existing or existing.get("_rev") != expected_rev):
            raise ValueError(f"File changed during write: {vault_path}")
        self.raw[vault_path] = raw
        doc = {
            "_id": self._doc_id(vault_path),
            "_rev": "1-new",
            "path": vault_path,
            "children": ["h:data"],
            "size": len(raw),
            "ctime": 1,
            "mtime": 2,
            "type": "plain" if is_text else "newnote",
        }
        if is_text:
            doc["content"] = raw.decode("utf-8")
        self.docs[vault_path.lower()] = doc
        return True

    async def write_note(self, path: str, content: str, is_binary: bool = False):
        if path in self.fail_writes:
            raise ValueError(f"write failed: {path}")
        self.writes.append((path, content, is_binary))
        doc = self.docs[path.lstrip("/").lower()]
        doc["content"] = content
        doc["size"] = len(content.encode("utf-8"))
        return True

    async def delete_note(self, path: str, hard: bool = False):
        doc = self.docs[path.lstrip("/").lower()]
        doc["deleted"] = True
        self.deleted.append((path, hard))
        return True


def _doc(path, doc_type="plain", **kwargs):
    doc = {
        "_id": path.lower(),
        "_rev": "1-doc",
        "path": path,
        "children": [],
        "size": 0,
        "ctime": 1,
        "mtime": 1,
        "type": doc_type,
    }
    doc.update(kwargs)
    return doc


def _next_rev(rev):
    if not rev:
        return "1-put"
    try:
        generation = int(str(rev).split("-", 1)[0])
    except ValueError:
        generation = 0
    return f"{generation + 1}-put"


def test_client_inherits_attachment_ops():
    client = ObsidianVaultClient(
        Config(
            couch_url="http://test:5984",
            couch_user="user",
            couch_pass="pass",
            db_name="test-vault",
        )
    )
    assert hasattr(client, "write_attachment")


async def test_write_and_read_attachment_roundtrip():
    client = _MemoryAttachmentClient()

    assert await client.write_attachment("Attachments/photo.png", b"hello") is True
    att = await client.read_attachment("Attachments/photo.png")

    assert att is not None
    assert att.path == "Attachments/photo.png"
    assert att.data == b"hello"
    assert att.size == 5
    assert att.content_type == "image/png"
    assert att.to_dict()["data_base64"] == base64.b64encode(b"hello").decode("ascii")


@pytest.mark.parametrize(
    "extension",
    [".md", ".txt", ".svg", ".html", ".csv", ".css", ".js", ".xml", ".canvas"],
)
async def test_write_attachment_rejects_livesync_plain_text_paths(extension):
    client = _MemoryAttachmentClient()

    with pytest.raises(ValueError, match="plain-text LiveSync file"):
        await client.write_attachment(f"Attachments/file{extension}", b"text")

    assert client.docs == {}


async def test_read_attachment_rejects_plain_note():
    client = _MemoryAttachmentClient([_doc("Notes/a.md", content="not binary")])

    with pytest.raises(ValueError, match="Not a binary attachment"):
        await client.read_attachment("Notes/a.md")


async def test_list_attachments_filters_sorts_and_paginates():
    client = _MemoryAttachmentClient(
        [
            _doc("Notes/a.md", "plain", mtime=400),
            _doc("Attachments/a.png", "newnote", size=10, mtime=200),
            _doc("Attachments/b.pdf", "newnote", size=20, mtime=300),
            _doc("Other/c.jpg", "newnote", size=30, mtime=500),
        ]
    )

    results = await client.list_attachments(folder="Attachments", limit=1, skip=1)

    assert len(results) == 1
    assert results[0].path == "Attachments/b.pdf"
    assert results[0].extension == "pdf"
    assert client.get_all_file_docs_calls == 0
    assert client.iter_file_docs_calls == 1


@respx.mock
async def test_list_attachments_requests_bounded_all_docs_pages():
    client = ObsidianVaultClient(
        Config(
            couch_url="http://test:5984",
            couch_user="user",
            couch_pass="pass",
            db_name="test-vault",
        )
    )
    docs = [
        _doc("Attachments/a.png", "newnote", size=10),
        _doc("Attachments/b.pdf", "newnote", size=20),
    ]
    seen_limits: list[str | None] = []

    def all_docs_page(request):
        seen_limits.append(request.url.params.get("limit"))
        return Response(200, json={"rows": [{"id": doc["_id"], "doc": doc} for doc in docs]})

    respx.get(f"{BASE}/_all_docs").mock(side_effect=all_docs_page)

    results = await client.list_attachments(folder="Attachments", limit=2)

    assert [attachment.path for attachment in results] == [
        "Attachments/a.png",
        "Attachments/b.pdf",
    ]
    assert seen_limits == ["2"]


@respx.mock
async def test_list_attachments_folder_filter_checks_slash_prefixed_range():
    client = ObsidianVaultClient(
        Config(
            couch_url="http://test:5984",
            couch_user="user",
            couch_pass="pass",
            db_name="test-vault",
        )
    )
    doc = _doc("Attachments/a.png", "newnote", _id="/attachments/a.png", size=10)
    seen_startkeys: list[str | None] = []

    def all_docs_page(request):
        startkey = request.url.params.get("startkey")
        seen_startkeys.append(startkey)
        if startkey == '"/attachments/"':
            return Response(200, json={"rows": [{"id": doc["_id"], "doc": doc}]})
        return Response(200, json={"rows": []})

    respx.get(f"{BASE}/_all_docs").mock(side_effect=all_docs_page)

    results = await client.list_attachments(folder="Attachments", limit=1)

    assert [attachment.path for attachment in results] == ["Attachments/a.png"]
    assert seen_startkeys == ['"attachments/"', '"/attachments/"']


async def test_find_attachment_embeds_matches_basename_refs():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/photo.png", "newnote"),
            _doc("Notes/a.md", content="Here ![[photo.png]]"),
            _doc("Notes/b.md", content="Here ![](other.png)"),
            _doc("Notes/c.md", content="Here [linked](Attachments/photo.png)"),
        ]
    )

    results = await client.find_attachment_embeds("Attachments/photo.png")

    assert [r.source_path for r in results] == ["Notes/a.md", "Notes/c.md"]
    assert "photo.png" in results[0].context
    assert client.get_all_file_docs_calls == 0
    assert client.iter_file_docs_calls == 1


async def test_find_attachment_embeds_includes_legacy_notes_docs():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/photo.png", "newnote"),
            _doc("Notes/legacy.md", "notes", data=["Here ![[photo.png]]"]),
        ]
    )

    results = await client.find_attachment_embeds("Attachments/photo.png")

    assert [r.source_path for r in results] == ["Notes/legacy.md"]


async def test_find_orphan_attachments():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/used.png", "newnote"),
            _doc("Attachments/orphan.pdf", "newnote"),
            _doc("Notes/a.md", content="![[used.png]]"),
        ]
    )

    results = await client.find_orphan_attachments()

    assert [r.path for r in results] == ["Attachments/orphan.pdf"]
    assert client.get_all_file_docs_calls == 0
    assert client.iter_file_docs_calls == 1


async def test_remove_attachment_blocks_when_embedded():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/used.png", "newnote"),
            _doc("Notes/a.md", content="![[used.png]]"),
        ]
    )

    result = await client.remove_attachment("Attachments/used.png")

    assert result == {"deleted": False, "referenced_by": ["Notes/a.md"]}
    assert client.deleted == []


async def test_remove_attachment_force_deletes():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/used.png", "newnote"),
            _doc("Notes/a.md", content="![[used.png]]"),
        ]
    )

    result = await client.remove_attachment("Attachments/used.png", force=True, hard=True)

    assert result == {"deleted": True, "referenced_by": ["Notes/a.md"]}
    assert client.deleted == [("Attachments/used.png", True)]


async def test_move_attachment_reuses_chunks_and_rewrites_links():
    client = _MemoryAttachmentClient(
        [
            _doc(
                "Attachments/old.png",
                "newnote",
                children=["h:img"],
                size=12,
                ctime=10,
                mtime=20,
            ),
            _doc("Notes/a.md", content="before ![[old.png|120]] after"),
            _doc("Notes/b.md", content="![cap](Attachments/old.png)"),
        ]
    )

    result = await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert result["moved"] is True
    assert result["new_path"] == "Media/new.png"
    assert result["links_rewritten"] == 2
    assert result["notes_updated"] == ["Notes/a.md", "Notes/b.md"]
    assert client.docs["attachments/old.png"]["deleted"] is True
    assert client.docs["media/new.png"]["children"] == ["h:img"]
    assert client.docs["notes/a.md"]["content"] == "before ![[new.png|120]] after"
    assert client.docs["notes/b.md"]["content"] == "![cap](Media/new.png)"


async def test_move_attachment_rewrites_current_note_content():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:img"]),
            _doc("Notes/a.md", content="before ![[old.png]]"),
        ]
    )

    def concurrent_note_edit(doc):
        if doc["path"] != "Media/new.png":
            return
        client.after_put = None
        note = client.docs["notes/a.md"]
        note["_rev"] = "2-doc"
        note["content"] = "concurrent edit ![[old.png]]"

    client.after_put = concurrent_note_edit

    await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert client.docs["notes/a.md"]["content"] == "concurrent edit ![[new.png]]"


async def test_move_attachment_aborts_when_source_changes_before_delete():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:old"]),
        ]
    )

    def concurrent_source_edit(doc):
        if doc["path"] != "Media/new.png":
            return
        client.after_put = None
        source = client.docs["attachments/old.png"]
        source["_rev"] = "2-doc"
        source["children"] = ["h:new"]
        source["size"] = 42

    client.after_put = concurrent_source_edit

    with pytest.raises(ValueError, match="changed during move"):
        await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert not client.docs["attachments/old.png"].get("deleted")
    assert client.docs["attachments/old.png"]["children"] == ["h:new"]
    assert client.docs["media/new.png"].get("deleted")


async def test_move_attachment_rewrite_failure_keeps_source_live():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:img"]),
            _doc("Notes/a.md", content="![[old.png]]"),
        ]
    )
    client.fail_writes.add("Notes/a.md")

    with pytest.raises(ValueError, match="write failed"):
        await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert not client.docs["attachments/old.png"].get("deleted")
    assert "media/new.png" not in client.docs or client.docs["media/new.png"].get("deleted")


async def test_move_attachment_rewrite_failure_restores_updated_notes():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:img"]),
            _doc("Notes/a.md", content="![[old.png]]"),
            _doc("Notes/b.md", content="![cap](Attachments/old.png)"),
        ]
    )
    client.fail_writes.add("Notes/b.md")

    with pytest.raises(ValueError, match="write failed"):
        await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert not client.docs["attachments/old.png"].get("deleted")
    assert "media/new.png" not in client.docs or client.docs["media/new.png"].get("deleted")
    assert client.docs["notes/a.md"]["content"] == "![[old.png]]"
    assert client.docs["notes/b.md"]["content"] == "![cap](Attachments/old.png)"


async def test_move_attachment_rollback_preserves_concurrent_note_edits():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:img"]),
            _doc("Notes/a.md", content="original ![[old.png]]"),
        ]
    )
    guarded_delete = client._soft_delete_doc_if_current

    async def fail_after_concurrent_note_edit(path, expected_rev):
        if path == "Media/new.png":
            await guarded_delete(path, expected_rev)
            return
        note = client.docs["notes/a.md"]
        note["_rev"] = "2-doc"
        note["content"] = "concurrent edit ![[new.png]]"
        raise ValueError(f"delete failed: {path} {expected_rev}")

    client._soft_delete_doc_if_current = fail_after_concurrent_note_edit

    with pytest.raises(ValueError, match="delete failed"):
        await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert not client.docs["attachments/old.png"].get("deleted")
    assert "media/new.png" not in client.docs or client.docs["media/new.png"].get("deleted")
    assert client.docs["notes/a.md"]["content"] == "concurrent edit ![[old.png]]"


async def test_move_attachment_rollback_preserves_concurrent_target_update():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:img"]),
        ]
    )
    guarded_deletes = []

    async def fail_after_concurrent_target_update(path, expected_rev):
        guarded_deletes.append((path, expected_rev))
        target = client.docs["media/new.png"]
        target["_rev"] = "2-target"
        target["children"] = ["h:other"]
        target["size"] = 42
        raise ValueError(f"delete failed: {path} {expected_rev}")

    client._soft_delete_doc_if_current = fail_after_concurrent_target_update

    with pytest.raises(ValueError, match="delete failed"):
        await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert not client.docs["attachments/old.png"].get("deleted")
    assert not client.docs["media/new.png"].get("deleted")
    assert client.docs["media/new.png"]["children"] == ["h:other"]
    assert client.docs["media/new.png"]["size"] == 42
    assert ("Media/new.png", "1-put") in guarded_deletes


async def test_move_attachment_rollback_preserves_concurrent_replaced_target_update():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:img"]),
            _doc("Media/new.png", "newnote", children=["h:stale"], deleted=True),
        ]
    )

    async def fail_after_concurrent_target_update(path, expected_rev):
        target = client.docs["media/new.png"]
        target["_rev"] = "3-target"
        target["deleted"] = False
        target["children"] = ["h:other"]
        target["size"] = 42
        raise ValueError(f"delete failed: {path} {expected_rev}")

    client._soft_delete_doc_if_current = fail_after_concurrent_target_update

    with pytest.raises(ValueError, match="delete failed"):
        await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert not client.docs["attachments/old.png"].get("deleted")
    assert not client.docs["media/new.png"].get("deleted")
    assert client.docs["media/new.png"]["children"] == ["h:other"]
    assert client.docs["media/new.png"]["size"] == 42


async def test_move_attachment_replacing_soft_deleted_target_leaves_stale_chunks_for_prune():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:new"]),
            _doc(
                "Media/new.png",
                "newnote",
                children=["h:stale"],
                deleted=True,
            ),
        ]
    )

    await client.move_attachment("Attachments/old.png", "Media/new.png")

    assert client.docs["media/new.png"]["children"] == ["h:new"]
    assert client.deleted_chunks == []


async def test_move_attachment_rejects_livesync_plain_text_target():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote", children=["h:img"]),
        ]
    )

    with pytest.raises(ValueError, match="plain-text LiveSync file"):
        await client.move_attachment("Attachments/old.png", "Media/new.svg")

    assert not client.docs["attachments/old.png"].get("deleted")
    assert "media/new.svg" not in client.docs


async def test_move_attachment_rejects_existing_target():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote"),
            _doc("Media/new.png", "newnote"),
        ]
    )

    with pytest.raises(ValueError, match="Target already exists"):
        await client.move_attachment("Attachments/old.png", "Media/new.png")
