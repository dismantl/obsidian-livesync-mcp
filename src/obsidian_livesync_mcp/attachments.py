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

_LIVESYNC_PLAIN_TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".svg",
    ".html",
    ".csv",
    ".css",
    ".js",
    ".xml",
    ".canvas",
}


class AttachmentOps:
    """Mixin implementing binary attachment operations."""

    async def write_attachment(self, path: str, data: bytes) -> bool:
        """Create or replace a binary attachment."""
        if _is_livesync_plain_text_path(path):
            raise ValueError(
                "add_attachment only supports binary attachments; use note tools "
                "for plain-text LiveSync files such as .svg, .txt, and .canvas"
            )
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
            content = await self._read_text_doc_content(doc)
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
            content = await self._read_text_doc_content(doc)
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
        if _is_livesync_plain_text_path(new_vault_path):
            raise ValueError(
                "move_attachment only supports binary attachment targets; use note tools "
                "for plain-text LiveSync files such as .svg, .txt, and .canvas"
            )
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

        note_paths: list[str] = []
        if rewrite_links:
            all_docs = await self._get_all_file_docs()
            for doc in all_docs:
                if doc.get("type") == "newnote":
                    continue
                content = await self._read_text_doc_content(doc)
                if not content:
                    continue
                new_content, count = rewrite_attachment_refs(content, old_path, new_vault_path)
                if count:
                    note_path = doc.get("path", doc["_id"])
                    if note_path not in note_paths:
                        note_paths.append(note_path)

        notes_updated: list[str] = []
        note_rollbacks: dict[str, tuple[str, str]] = {}
        links_rewritten = 0
        source_rev = old_doc.get("_rev")
        resp = await client.put(f"/{encode_doc_id(new_doc['_id'])}", json=new_doc)
        resp.raise_for_status()
        target_rev = _response_rev(resp)

        try:
            for note_path in note_paths:
                rewrite = await self._rewrite_current_note_refs(note_path, old_path, new_vault_path)
                if not rewrite:
                    continue
                original_content, rewritten_content, count = rewrite
                note_rollbacks[note_path] = (original_content, rewritten_content)
                notes_updated.append(note_path)
                links_rewritten += count
            await self._soft_delete_doc_if_current(old_path, source_rev)
        except Exception:
            await self._rollback_move_failure(
                old_path,
                new_vault_path,
                existing_new,
                target_rev,
                note_rollbacks,
                notes_updated,
            )
            raise

        return {
            "moved": True,
            "new_path": new_vault_path,
            "links_rewritten": links_rewritten,
            "notes_updated": notes_updated,
        }

    async def _rollback_move_failure(
        self,
        old_path: str,
        new_path: str,
        existing_target: dict | None,
        target_rev: str | None,
        note_rollbacks: dict[str, tuple[str, str]],
        notes_updated: list[str],
    ) -> None:
        for note_path in reversed(notes_updated):
            try:
                await self._rollback_note_refs(
                    note_path,
                    old_path,
                    new_path,
                    note_rollbacks[note_path],
                )
            except Exception:
                logger.warning(
                    "Failed to restore note %s while rolling back attachment move",
                    note_path,
                    exc_info=True,
                )

        try:
            if not target_rev:
                raise ValueError(f"Cannot safely roll back target without revision: {new_path}")
            if existing_target:
                await self._restore_file_doc(new_path, existing_target, expected_rev=target_rev)
            else:
                await self._soft_delete_doc_if_current(new_path, target_rev)
        except Exception:
            logger.warning(
                "Failed to restore target %s while rolling back attachment move",
                new_path,
                exc_info=True,
            )

    async def _rollback_note_refs(
        self,
        note_path: str,
        old_path: str,
        new_path: str,
        rollback: tuple[str, str],
    ) -> None:
        doc = await self._get_doc(note_path)
        if not doc or doc.get("deleted") or doc.get("type") == "newnote":
            return

        content = await self._read_text_doc_content(doc)
        if content is None:
            return

        original_content, rewritten_content = rollback
        if content == rewritten_content:
            restored_content = original_content
        else:
            restored_content, count = rewrite_attachment_refs(content, new_path, old_path)
            if not count:
                return

        expected_rev = doc.get("_rev")
        await self._write_file_doc(
            note_path,
            restored_content.encode("utf-8"),
            is_text=True,
            expected_rev=expected_rev,
        )

    async def _restore_file_doc(
        self,
        path: str,
        previous_doc: dict,
        expected_rev: str | None = None,
    ) -> None:
        client = await self._get_client()
        current = await self._get_doc(path)
        if expected_rev and (not current or current.get("_rev") != expected_rev):
            raise ValueError(f"Target changed during move rollback: {path}")
        restored = dict(previous_doc)
        if current and current.get("_rev"):
            restored["_rev"] = current["_rev"]
        resp = await client.put(f"/{encode_doc_id(restored['_id'])}", json=restored)
        resp.raise_for_status()

    async def _rewrite_current_note_refs(
        self,
        note_path: str,
        old_path: str,
        new_path: str,
    ) -> tuple[str, str, int] | None:
        doc = await self._get_doc(note_path)
        if not doc or doc.get("deleted") or doc.get("type") == "newnote":
            return None
        content = await self._read_text_doc_content(doc)
        if not content:
            return None
        new_content, count = rewrite_attachment_refs(content, old_path, new_path)
        if not count:
            return None
        expected_rev = doc.get("_rev")
        await self._write_file_doc(
            note_path,
            new_content.encode("utf-8"),
            is_text=True,
            expected_rev=expected_rev,
        )
        return content, new_content, count

    async def _soft_delete_doc_if_current(self, path: str, expected_rev: str | None) -> None:
        client = await self._get_client()
        doc = await self._get_doc(path)
        if not doc or doc.get("deleted"):
            raise ValueError(f"Attachment not found during move: {path}")
        if expected_rev and doc.get("_rev") != expected_rev:
            raise ValueError(f"Attachment changed during move: {path}")
        doc["deleted"] = True
        doc["mtime"] = int(time.time() * 1000)
        resp = await client.put(f"/{encode_doc_id(doc['_id'])}", json=doc)
        if getattr(resp, "status_code", None) == 409:
            raise ValueError(f"Attachment changed during move: {path}")
        resp.raise_for_status()

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

    async def _read_text_doc_content(self, doc: dict) -> str | None:
        if doc.get("type") == "notes":
            data = doc.get("data", "")
            return "".join(data) if isinstance(data, list) else str(data)
        return await self._read_note_content(doc)


def _is_livesync_plain_text_path(path: str) -> bool:
    return path.lower().endswith(tuple(_LIVESYNC_PLAIN_TEXT_EXTENSIONS))


def _response_rev(resp) -> str | None:
    try:
        body = resp.json()
    except Exception:
        return None
    rev = body.get("rev") if isinstance(body, dict) else None
    return rev if isinstance(rev, str) else None
