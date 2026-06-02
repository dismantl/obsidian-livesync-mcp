"""Attachment operations for binary LiveSync file docs."""

import base64
import logging
import mimetypes
import time

from .models import AttachmentContent, AttachmentMetadata, BacklinkInfo
from .utils import (
    encode_doc_id,
    extract_attachment_refs,
    ref_basename,
    rewrite_attachment_refs,
)

logger = logging.getLogger(__name__)


class AttachmentOps:
    """Mixin implementing binary attachment operations."""

    async def write_attachment(self, path: str, data: bytes) -> bool:
        """Create or replace a binary attachment."""
        return await self._write_file_doc(path, data, is_text=False)

    async def read_attachment(self, path: str) -> AttachmentContent | None:
        """Read a binary attachment as bytes."""
        note = await self.read_note(path)
        if note is None:
            return None
        if not note.is_binary:
            raise ValueError(f"Not a binary attachment: {path}")

        data = base64.b64decode(note.content)
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return AttachmentContent(
            path=note.path,
            data=data,
            size=len(data),
            content_type=content_type,
        )

    async def get_attachment_metadata(self, path: str) -> AttachmentMetadata | None:
        """Read attachment metadata without fetching chunk bytes."""
        doc = await self._get_doc(path)
        if not doc or doc.get("deleted"):
            return None
        if doc.get("type") != "newnote":
            raise ValueError(f"Not a binary attachment: {path}")
        return self._attachment_metadata(doc)

    async def list_attachments(
        self, folder: str | None = None, limit: int = 100, skip: int = 0
    ) -> list[AttachmentMetadata]:
        """List binary attachment metadata."""
        all_docs = await self._get_all_file_docs()
        docs = [doc for doc in all_docs if doc.get("type") == "newnote"]

        if folder:
            folder_lower = folder.strip("/").lower() + "/"
            docs = [
                doc
                for doc in docs
                if doc.get("path", doc.get("_id", "")).lower().startswith(folder_lower)
            ]

        docs.sort(key=lambda doc: doc.get("mtime", 0), reverse=True)
        return [self._attachment_metadata(doc) for doc in docs[skip : skip + limit]]

    async def find_attachment_embeds(self, path: str) -> list[BacklinkInfo]:
        """Find notes that reference an attachment by basename."""
        target_base = ref_basename(path)
        all_docs = await self._get_all_file_docs()
        results: list[BacklinkInfo] = []

        for doc in all_docs:
            if doc.get("type") == "newnote":
                continue
            content = await self._read_note_content(doc)
            if not content:
                continue
            refs = extract_attachment_refs(content)
            matching_refs = [ref for ref in refs if ref_basename(ref) == target_base]
            if not matching_refs:
                continue
            context = self._attachment_context(content, matching_refs)
            results.append(BacklinkInfo(source_path=doc.get("path", doc["_id"]), context=context))

        return results

    async def find_orphan_attachments(self, folder: str | None = None) -> list[AttachmentMetadata]:
        """Find attachments that no note references."""
        all_docs = await self._get_all_file_docs()
        attachments = [doc for doc in all_docs if doc.get("type") == "newnote"]
        notes = [doc for doc in all_docs if doc.get("type") != "newnote"]

        if folder:
            folder_lower = folder.strip("/").lower() + "/"
            attachments = [
                doc
                for doc in attachments
                if doc.get("path", doc.get("_id", "")).lower().startswith(folder_lower)
            ]

        referenced: set[str] = set()
        for doc in notes:
            content = await self._read_note_content(doc)
            if not content:
                continue
            referenced.update(ref_basename(ref) for ref in extract_attachment_refs(content))

        attachments.sort(key=lambda doc: doc.get("mtime", 0), reverse=True)
        return [
            self._attachment_metadata(doc)
            for doc in attachments
            if ref_basename(doc.get("path", doc["_id"])) not in referenced
        ]

    async def remove_attachment(self, path: str, hard: bool = False, force: bool = False) -> dict:
        """Remove an attachment, guarding against live references by default."""
        doc = await self._get_doc(path)
        if not doc or doc.get("deleted"):
            raise ValueError(f"Attachment not found: {path}")
        if doc.get("type") != "newnote":
            raise ValueError(f"Not a binary attachment: {path}")

        refs = await self.find_attachment_embeds(path)
        referenced_by = [ref.source_path for ref in refs]
        if referenced_by and not force:
            return {"deleted": False, "referenced_by": referenced_by}

        await self.delete_note(path, hard=hard)
        return {"deleted": True, "referenced_by": referenced_by}

    async def move_attachment(
        self, old_path: str, new_path: str, rewrite_links: bool = True
    ) -> dict:
        """Move a binary attachment and optionally rewrite note references."""
        client = await self._get_client()

        old_doc = await self._get_doc(old_path)
        if not old_doc or old_doc.get("deleted"):
            raise ValueError(f"Attachment not found: {old_path}")
        if old_doc.get("type") != "newnote":
            raise ValueError(f"Not a binary attachment: {old_path}")

        new_vault_path = new_path.lstrip("/")
        existing_new = await self._get_doc(new_vault_path)
        if existing_new and not existing_new.get("deleted"):
            raise ValueError(f"Target already exists: {new_path}")

        now_ms = int(time.time() * 1000)
        new_doc = {
            "_id": self._doc_id(new_vault_path),
            "children": list(old_doc.get("children", [])),
            "path": new_vault_path,
            "ctime": old_doc.get("ctime", now_ms),
            "mtime": now_ms,
            "size": old_doc.get("size", 0),
            "type": "newnote",
            "eden": {},
        }
        if existing_new:
            new_doc["_id"] = existing_new["_id"]
            new_doc["_rev"] = existing_new["_rev"]

        resp = await client.put(f"/{encode_doc_id(new_doc['_id'])}", json=new_doc)
        resp.raise_for_status()

        await self.delete_note(old_path, hard=False)

        notes_updated: list[str] = []
        links_rewritten = 0
        if rewrite_links:
            all_docs = await self._get_all_file_docs()
            for doc in all_docs:
                if doc.get("type") == "newnote":
                    continue
                content = await self._read_note_content(doc)
                if not content:
                    continue
                new_content, count = rewrite_attachment_refs(content, old_path, new_vault_path)
                if count:
                    note_path = doc.get("path", doc["_id"])
                    await self.write_note(note_path, new_content)
                    notes_updated.append(note_path)
                    links_rewritten += count

        return {
            "moved": True,
            "new_path": new_vault_path,
            "links_rewritten": links_rewritten,
            "notes_updated": notes_updated,
        }

    def _attachment_metadata(self, doc: dict) -> AttachmentMetadata:
        path = doc.get("path", doc["_id"])
        basename = path.rsplit("/", 1)[-1]
        extension = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
        return AttachmentMetadata(
            path=path,
            size=doc.get("size", 0),
            ctime=doc.get("ctime", 0),
            mtime=doc.get("mtime", 0),
            extension=extension,
            chunk_count=len(doc.get("children", [])),
        )

    def _attachment_context(self, content: str, refs: list[str]) -> str:
        for line in content.splitlines():
            if any(ref in line for ref in refs):
                return line.strip()
        return ""
