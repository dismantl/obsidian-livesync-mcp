"""Tests for attachment operations."""

import base64

import pytest

from obsidian_livesync_mcp.attachments import AttachmentOps
from obsidian_livesync_mcp.client import ObsidianVaultClient
from obsidian_livesync_mcp.config import Config
from obsidian_livesync_mcp.models import NoteContent


class _Response:
    def raise_for_status(self):
        return None


class _PutClient:
    def __init__(self, owner):
        self.owner = owner

    async def put(self, _url, json):
        path = json["path"].lstrip("/")
        self.owner.docs[path.lower()] = json
        self.owner.put_docs.append(json)
        return _Response()


class _MemoryAttachmentClient(AttachmentOps):
    def __init__(self, docs=None, raw=None):
        self.docs = {doc["path"].lower(): dict(doc) for doc in docs or []}
        self.raw = raw or {}
        self.deleted = []
        self.writes = []
        self.put_docs = []

    async def _get_client(self):
        return _PutClient(self)

    def _doc_id(self, vault_path: str) -> str:
        return vault_path.lstrip("/").lower()

    async def _get_doc(self, path: str):
        return self.docs.get(path.lstrip("/").lower())

    async def _get_all_file_docs(self, include_deleted: bool = False):
        return [doc for doc in self.docs.values() if include_deleted or not doc.get("deleted")]

    async def _read_note_content(self, doc):
        return doc.get("content")

    async def _reassemble_binary(self, doc, chunks=None):
        return self.raw.get(doc["path"], b"")

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

    async def _write_file_doc(self, path: str, raw: bytes, is_text: bool):
        vault_path = path.lstrip("/")
        self.raw[vault_path] = raw
        self.docs[vault_path.lower()] = {
            "_id": self._doc_id(vault_path),
            "_rev": "1-new",
            "path": vault_path,
            "children": ["h:data"],
            "size": len(raw),
            "ctime": 1,
            "mtime": 2,
            "type": "plain" if is_text else "newnote",
        }
        return True

    async def write_note(self, path: str, content: str, is_binary: bool = False):
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
    assert results[0].path == "Attachments/a.png"
    assert results[0].extension == "png"


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


async def test_move_attachment_rejects_existing_target():
    client = _MemoryAttachmentClient(
        [
            _doc("Attachments/old.png", "newnote"),
            _doc("Media/new.png", "newnote"),
        ]
    )

    with pytest.raises(ValueError, match="Target already exists"):
        await client.move_attachment("Attachments/old.png", "Media/new.png")
