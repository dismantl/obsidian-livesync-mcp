"""Async CouchDB client for Obsidian vault operations."""

import asyncio
import base64
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

import httpx

from .attachments import AttachmentOps
from .chunking import decode_binary_chunks, split_chunks
from .config import Config
from .models import BacklinkInfo, FolderInfo, NoteContent, NoteMetadata, SearchResult
from .utils import (
    encode_doc_id,
    extract_frontmatter,
    extract_tags,
    extract_wikilinks,
    generate_chunk_id,
    normalize_doc_id,
    set_frontmatter,
)

logger = logging.getLogger(__name__)
BINARY_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".pdf",
    ".mp3",
    ".mp4",
    ".wav",
    ".zip",
    ".tar",
    ".gz",
}

# read_note tolerates a chunk that is momentarily missing — mid-replication, or
# cleaned up during a concurrent rewrite — by re-fetching the parent and retrying
# a few times before failing. This mirrors the Obsidian app, which waits for
# chunks rather than erroring on a transient gap.
READ_RETRIES = 3
READ_RETRY_DELAY = 0.25


@dataclass
class PruneReport:
    total_chunks: int
    referenced: int
    orphan_chunk_ids: list[str]
    deleted: int


class ObsidianVaultClient(AttachmentOps):
    """Async client for reading/writing Obsidian vault docs in CouchDB."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.config.db_url,
                auth=(self.config.couch_user, self.config.couch_pass),
                headers={"Content-Type": "application/json"},
                timeout=30.0,
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Low-level helpers ──────────────────────────────────────────

    def _doc_id(self, vault_path: str) -> str:
        """Generate CouchDB doc ID for a vault path, respecting obfuscation config."""
        return normalize_doc_id(
            vault_path,
            obfuscate_passphrase=self.config.obfuscate_passphrase,
        )

    async def _get_doc(self, path: str) -> dict | None:
        """Fetch a doc by vault path, trying both ID conventions."""
        client = await self._get_client()
        doc_id = self._doc_id(path)

        # Try normalized ID first (handles '_' prefix → '/_' automatically)
        resp = await client.get(f"/{encode_doc_id(doc_id)}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code != 404:
            resp.raise_for_status()

        # Try alternate convention (with/without leading slash)
        alt_id = "/" + doc_id if not doc_id.startswith("/") else doc_id[1:]
        resp = await client.get(f"/{encode_doc_id(alt_id)}")
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code != 404:
            resp.raise_for_status()

        return None

    async def _fetch_chunks(self, chunk_ids: list[str]) -> dict[str, str]:
        """Batch-fetch chunks via POST _all_docs. Returns {chunk_id: data}."""
        if not chunk_ids:
            return {}
        client = await self._get_client()
        resp = await client.post(
            "/_all_docs",
            json={"keys": chunk_ids},
            params={"include_docs": "true"},
        )
        resp.raise_for_status()
        result = {}
        for row in resp.json().get("rows", []):
            doc = row.get("doc")
            if doc and "data" in doc:
                result[row["id"]] = doc["data"]
        return result

    async def _put_chunk_doc(self, chunk_id: str, chunk_data: str) -> None:
        """Ensure a chunk doc exists and is live before any parent references it."""
        client = await self._get_client()
        encoded_id = encode_doc_id(chunk_id)
        body = {"_id": chunk_id, "data": chunk_data, "type": "leaf"}

        resp = await client.put(f"/{encoded_id}", json=body)
        if resp.status_code != 409:
            resp.raise_for_status()
            return

        existing = await self._get_chunk_doc_for_conflict(chunk_id)
        doc = existing.get("doc") or {}
        value = existing.get("value") or {}
        if doc and not doc.get("_deleted"):
            if doc.get("data") == chunk_data:
                return
            raise ValueError(f"Chunk ID collision for {chunk_id}")

        rev = doc.get("_rev") or value.get("rev")
        deleted = bool(doc.get("_deleted") or value.get("deleted"))
        if not deleted:
            retry_resp = await client.put(f"/{encoded_id}", json=body)
            if retry_resp.status_code != 409:
                retry_resp.raise_for_status()
                return
            raise ValueError(f"Chunk {chunk_id} conflicted but no live doc or tombstone exists")
        if not rev:
            raise ValueError(f"Chunk {chunk_id} conflicted without a reusable _rev")

        resurrect_body = body | {"_rev": rev}
        resurrect_resp = await client.put(f"/{encoded_id}", json=resurrect_body)
        if resurrect_resp.status_code == 409:
            verified = await self._get_chunk_doc_for_conflict(chunk_id)
            verified_doc = verified.get("doc") or {}
            if not verified_doc.get("_deleted") and verified_doc.get("data") == chunk_data:
                return
            raise ValueError(f"Chunk {chunk_id} changed during tombstone resurrection")
        resurrect_resp.raise_for_status()

    async def _get_chunk_doc_for_conflict(self, chunk_id: str) -> dict:
        """Fetch live chunk data or deleted tombstone rev after a chunk 409."""
        client = await self._get_client()
        resp = await client.post(
            "/_all_docs",
            json={"keys": [chunk_id]},
            params={"include_docs": "true"},
        )
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        if not rows:
            return {}
        row = rows[0]
        return {} if row.get("error") == "not_found" else row

    async def _reassemble_binary(self, doc: dict, chunks: dict[str, str] | None = None) -> bytes:
        """Fetch and reassemble a binary doc's original bytes."""
        chunk_ids = doc.get("children", [])
        if not chunk_ids:
            return b""
        chunks = chunks if chunks is not None else await self._fetch_chunks(chunk_ids)
        missing = [cid for cid in chunk_ids if cid not in chunks]
        if missing:
            doc_id = doc.get("_id", "unknown")
            raise ValueError(f"Missing {len(missing)} chunk(s) for {doc_id}: {missing[:3]}")
        return decode_binary_chunks([chunks[cid] for cid in chunk_ids])

    async def _delete_orphan_chunks(self, chunk_ids: list[str]) -> int:
        """Delete orphaned chunk documents and return the number deleted."""
        client = await self._get_client()
        deleted = 0
        for chunk_id in chunk_ids:
            try:
                resp = await client.get(f"/{encode_doc_id(chunk_id)}")
                if resp.status_code == 200:
                    chunk_rev = resp.json().get("_rev")
                    del_resp = await client.delete(
                        f"/{encode_doc_id(chunk_id)}",
                        params={"rev": chunk_rev},
                    )
                    if del_resp.status_code not in (200, 202):
                        logger.warning("Failed to delete orphan chunk %s", chunk_id)
                    else:
                        deleted += 1
                elif resp.status_code != 404:
                    logger.warning(
                        "Failed to fetch orphan chunk %s: %s", chunk_id, resp.status_code
                    )
            except Exception:
                logger.warning("Error cleaning up orphan chunk %s", chunk_id, exc_info=True)
        return deleted

    async def _collect_chunks_in_use_by_other_docs(self, exclude_doc_id: str) -> set[str]:
        """Return all chunk IDs referenced by file docs other than exclude_doc_id.

        Chunks are content-addressed and deduplicated: two notes with identical
        content share the same chunk ID. Orphan cleanup on write/delete must
        consult this set before deleting a chunk, or it will break the other notes.
        """
        all_docs = await self._get_all_file_docs(include_deleted=True)
        in_use: set[str] = set()
        for doc in all_docs:
            if doc.get("_id") == exclude_doc_id:
                continue
            in_use.update(doc.get("children", []))
        return in_use

    async def _get_all_file_docs(self, include_deleted: bool = False) -> list[dict]:
        """Fetch all file docs (skip chunks, design docs, index docs).

        By default, excludes LiveSync soft-deleted docs (``deleted: True``).
        Pass ``include_deleted=True`` to include them (e.g. for orphan-chunk
        bookkeeping where we need the full set).
        """
        client = await self._get_client()
        docs = []

        # Range 1: docs before "h:" (chunk prefix)
        resp = await client.get(
            "/_all_docs",
            params={
                "include_docs": "true",
                "endkey": '"h:"',
                "inclusive_end": "false",
            },
        )
        resp.raise_for_status()
        for row in resp.json().get("rows", []):
            doc = row.get("doc", {})
            if doc.get("type") in ("plain", "newnote", "notes") and (
                "children" in doc or "data" in doc
            ):
                if not include_deleted and doc.get("deleted"):
                    continue
                docs.append(doc)

        # Range 2: docs after "h:~" (after all chunks)
        resp = await client.get(
            "/_all_docs",
            params={
                "include_docs": "true",
                "startkey": '"h:~"',
            },
        )
        resp.raise_for_status()
        for row in resp.json().get("rows", []):
            doc = row.get("doc", {})
            if doc.get("type") in ("plain", "newnote", "notes") and (
                "children" in doc or "data" in doc
            ):
                if not include_deleted and doc.get("deleted"):
                    continue
                docs.append(doc)

        return docs

    async def prune_orphan_chunks(self, *, dry_run: bool = True) -> PruneReport:
        """Find chunk docs referenced by no live or soft-deleted file doc.

        This is the MCP analog of upstream LiveSync's manual Garbage Collection:
        deletion tombstones chunks, which is unsafe if another device still
        references one. Sync all devices first before opting into deletion. This
        scan inspects current file docs' children only, not document history or
        other devices' pending writes, so prefer the app's own GC / rebuild path
        when an Obsidian client is available.
        """
        client = await self._get_client()
        resp = await client.get(
            "/_all_docs",
            params={
                "startkey": '"h:"',
                "endkey": '"h:~"',
                "inclusive_end": "false",
            },
        )
        resp.raise_for_status()
        all_chunk_ids = [row["id"] for row in resp.json().get("rows", []) if "id" in row]

        in_use: set[str] = set()
        for doc in await self._get_all_file_docs(include_deleted=True):
            in_use.update(doc.get("children", []))

        orphan_chunk_ids = [chunk_id for chunk_id in all_chunk_ids if chunk_id not in in_use]
        deleted = 0
        if not dry_run and orphan_chunk_ids:
            deleted = await self._delete_orphan_chunks(orphan_chunk_ids)

        return PruneReport(
            total_chunks=len(all_chunk_ids),
            referenced=len(all_chunk_ids) - len(orphan_chunk_ids),
            orphan_chunk_ids=orphan_chunk_ids,
            deleted=deleted,
        )

    # ── Read operations ────────────────────────────────────────────

    async def list_notes(
        self, folder: str | None = None, limit: int = 50, skip: int = 0
    ) -> list[NoteMetadata]:
        """List notes, optionally filtered by folder prefix."""
        all_docs = await self._get_all_file_docs()

        if folder:
            folder_lower = folder.strip("/").lower() + "/"
            all_docs = [
                d for d in all_docs if d.get("_id", "").lstrip("/").startswith(folder_lower)
            ]

        # Sort by mtime descending
        all_docs.sort(key=lambda d: d.get("mtime", 0), reverse=True)

        results = []
        for doc in all_docs[skip : skip + limit]:
            results.append(
                NoteMetadata(
                    path=doc.get("path", doc["_id"]),
                    size=doc.get("size", 0),
                    ctime=doc.get("ctime", 0),
                    mtime=doc.get("mtime", 0),
                    doc_type=doc.get("type", "plain"),
                    chunk_count=len(doc.get("children", [])),
                )
            )
        return results

    async def read_note(
        self,
        path: str,
        *,
        retries: int = READ_RETRIES,
        retry_delay: float = READ_RETRY_DELAY,
    ) -> NoteContent | None:
        """Read a note's full content by reassembling chunks in order.

        A note written concurrently can momentarily reference a chunk that is
        still mid-replication, or one that was just cleaned up during a rewrite
        (the reader holds a stale parent while a writer swapped it and deleted
        the old chunk). Rather than fail on that transient gap — unlike the
        Obsidian app, which waits for chunks — re-fetch the parent fresh (so a
        rewrite's new ``children`` get resolved) and retry up to ``retries``
        times, ``retry_delay`` seconds apart.

        Raises ValueError only if chunks are still missing after the final
        attempt (e.g. a genuinely broken manifest). For bulk scans that should
        skip broken notes instead of raising, use _read_note_content.
        """
        last_missing: list[str] = []
        for attempt in range(retries + 1):
            doc = await self._get_doc(path)
            if not doc or doc.get("deleted"):
                return None

            is_binary = doc.get("type") == "newnote"

            # Legacy "notes" type stores content directly in data field
            if doc.get("type") == "notes":
                data = doc.get("data", "")
                content = "".join(data) if isinstance(data, list) else str(data)
            else:
                chunk_ids = doc.get("children", [])
                chunks = await self._fetch_chunks(chunk_ids)
                missing = [cid for cid in chunk_ids if cid not in chunks]
                if missing:
                    last_missing = missing
                    if attempt < retries:
                        await asyncio.sleep(retry_delay)
                    continue
                if is_binary:
                    raw = await self._reassemble_binary(doc, chunks)
                    content = base64.b64encode(raw).decode("ascii")
                    size = len(raw)
                else:
                    content = "".join(chunks[cid] for cid in chunk_ids)
                    size = doc.get("size", 0)

            return NoteContent(
                path=doc.get("path", path),
                content=content,
                size=size if is_binary else doc.get("size", 0),
                is_binary=is_binary,
            )

        raise ValueError(
            f"Missing {len(last_missing)} chunk(s) for {path} after "
            f"{retries + 1} attempt(s): {last_missing[:3]}"
        )

    async def list_folders(self) -> list[FolderInfo]:
        """Extract unique folder paths from all file docs."""
        all_docs = await self._get_all_file_docs()
        folder_counts: dict[str, int] = defaultdict(int)

        for doc in all_docs:
            path = doc.get("path", doc.get("_id", ""))
            parts = path.rsplit("/", 1)
            if len(parts) == 2:
                folder = parts[0]
                folder_counts[folder] += 1
            else:
                folder_counts["(root)"] += 1

        results = [FolderInfo(path=f, note_count=c) for f, c in sorted(folder_counts.items())]
        return results

    # ── Write operations ───────────────────────────────────────────

    async def write_note(self, path: str, content: str | bytes, is_binary: bool = False) -> bool:
        """Create or update a note. Returns True on success."""
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        return await self._write_file_doc(path, raw, is_text=not is_binary)

    async def _write_file_doc(
        self,
        path: str,
        raw: bytes,
        is_text: bool,
        expected_rev: str | None = None,
    ) -> bool:
        """Create/update a file doc from raw bytes using LiveSync chunk docs."""
        client = await self._get_client()
        vault_path = path.lstrip("/")
        doc_id = self._doc_id(vault_path)
        encoded_id = encode_doc_id(doc_id)

        file_size = len(raw)
        doc_type = "plain" if is_text else "newnote"
        chunks_data = split_chunks(raw, is_text=is_text)

        # Create chunk docs with content-hash IDs
        chunk_ids = []
        for chunk_data in chunks_data:
            chunk_id = generate_chunk_id(chunk_data)
            await self._put_chunk_doc(chunk_id, chunk_data)
            chunk_ids.append(chunk_id)

        now_ms = int(time.time() * 1000)

        # Check existing doc
        existing = await self._get_doc(vault_path)
        if expected_rev and (not existing or existing.get("_rev") != expected_rev):
            raise ValueError(f"File changed during write: {vault_path}")

        if existing:
            existing["children"] = chunk_ids
            existing["mtime"] = now_ms
            existing["size"] = file_size
            existing["type"] = doc_type
            existing.pop("deleted", None)
            # Use the existing _id for the PUT
            existing_id = encode_doc_id(existing["_id"])
            resp = await client.put(f"/{existing_id}", json=existing)
            if resp.status_code == 409:
                if expected_rev:
                    raise ValueError(f"File changed during write: {vault_path}")
                # Conflict - refetch and retry once
                fresh = await self._get_doc(vault_path)
                if fresh:
                    fresh["children"] = chunk_ids
                    fresh["mtime"] = now_ms
                    fresh["size"] = file_size
                    fresh["type"] = doc_type
                    fresh.pop("deleted", None)
                    fresh_id = encode_doc_id(fresh["_id"])
                    resp = await client.put(f"/{fresh_id}", json=fresh)
                else:
                    raise ValueError(f"Note was deleted during write: {vault_path}")
            resp.raise_for_status()
        else:
            new_doc = {
                "_id": doc_id,
                "children": chunk_ids,
                "path": vault_path,
                "ctime": now_ms,
                "mtime": now_ms,
                "size": file_size,
                "type": doc_type,
                "eden": {},
            }
            resp = await client.put(f"/{encoded_id}", json=new_doc)
            resp.raise_for_status()

        # NOTE: Automatic orphan-chunk deletion was intentionally removed
        # (2026-06-15). Deleting content-addressed chunks creates CouchDB
        # tombstones that can break other notes and are sticky at the
        # replication layer. Pruning is now an explicit, dry-run-default
        # maintenance command; see prune_orphan_chunks / CLI prune-orphans.

        return True

    async def append_note(self, path: str, content: str) -> bool:
        """Append content to an existing note. Returns True on success."""
        client = await self._get_client()

        doc = await self._get_doc(path)
        if not doc:
            raise ValueError(f"Note not found: {path}")

        # Clear tombstone flag if present (same fix as write_note)
        doc.pop("deleted", None)

        children = doc.get("children", [])
        if not children:
            raise ValueError(f"Note has no chunks: {path}")

        # Fetch all chunks to compute total size
        chunks = await self._fetch_chunks(children)

        # Get last chunk and append
        last_chunk_id = children[-1]
        if last_chunk_id not in chunks:
            raise ValueError(f"Last chunk missing for {path}: {last_chunk_id}")
        last_data = chunks[last_chunk_id]
        new_data = last_data + content

        # Create new chunk with appended content
        new_chunk_id = generate_chunk_id(new_data)
        resp = await client.put(
            f"/{encode_doc_id(new_chunk_id)}",
            json={"_id": new_chunk_id, "data": new_data, "type": "leaf"},
        )
        resp.raise_for_status()

        # Compute total size
        total_size = 0
        for cid in children:
            if cid == last_chunk_id:
                total_size += len(new_data.encode("utf-8"))
            else:
                total_size += len(chunks[cid].encode("utf-8"))

        # Update doc
        doc["children"][-1] = new_chunk_id
        doc["mtime"] = int(time.time() * 1000)
        doc["size"] = total_size

        doc_encoded = encode_doc_id(doc["_id"])
        resp = await client.put(f"/{doc_encoded}", json=doc)
        if resp.status_code == 409:
            fresh = await self._get_doc(path)
            if not fresh:
                raise ValueError(f"Note was deleted during append: {path}")
            fresh_children = fresh.get("children", [])
            if not fresh_children or fresh_children[-1] != last_chunk_id:
                raise ValueError(f"Conflict: note {path} was modified concurrently. Please retry.")
            fresh["children"][-1] = new_chunk_id
            fresh["mtime"] = int(time.time() * 1000)
            fresh["size"] = total_size
            fresh_id = encode_doc_id(fresh["_id"])
            resp = await client.put(f"/{fresh_id}", json=fresh)
        resp.raise_for_status()
        return True

    async def delete_note(self, path: str, hard: bool = False) -> bool:
        """Delete a note. Defaults to a livesync-compatible soft-delete.

        Soft-delete (default): sets `deleted: True` on the parent doc and
        bumps `mtime`, preserving chunks. This matches obsidian-livesync's own
        delete flow (`deleteDBEntryByPath` in `EntryManagerImpls.ts`) — livesync's
        apply-to-storage path only cleans up filesystem copies when the doc is
        still retrievable from CouchDB with the `deleted` field set. A CouchDB
        hard-delete tombstone is invisible to that path, so filesystem copies
        orphan on every device.

        Hard-delete (`hard=True`): standard CouchDB DELETE of the parent doc
        plus orphan chunk cleanup. Creates a `_deleted: True` tombstone. Use
        only for broken-manifest cleanup (missing-chunk recovery) — this form
        does NOT propagate to filesystem copies on livesync-connected devices.
        """
        client = await self._get_client()

        doc = await self._get_doc(path)
        if not doc:
            raise ValueError(f"Note not found: {path}")

        if not hard:
            # Soft-delete: flag + bump mtime, leave chunks alone.
            now_ms = int(time.time() * 1000)
            doc["deleted"] = True
            doc["mtime"] = now_ms
            doc_encoded = encode_doc_id(doc["_id"])
            resp = await client.put(f"/{doc_encoded}", json=doc)
            if resp.status_code == 409:
                # Conflict — refetch and retry once (same shape as write_note)
                fresh = await self._get_doc(path)
                if not fresh:
                    return True  # Already gone — idempotent success
                fresh["deleted"] = True
                fresh["mtime"] = now_ms
                fresh_id = encode_doc_id(fresh["_id"])
                resp = await client.put(f"/{fresh_id}", json=fresh)
            resp.raise_for_status()
            return True

        # Hard-delete: chunk cleanup + CouchDB DELETE tombstone. Skip any chunk
        # still referenced by other notes (chunks are content-addressed and
        # deduplicated across the vault).
        chunk_ids = doc.get("children", [])
        in_use_elsewhere = (
            await self._collect_chunks_in_use_by_other_docs(doc["_id"]) if chunk_ids else set()
        )
        failed_chunks = []
        for chunk_id in chunk_ids:
            if chunk_id in in_use_elsewhere:
                continue
            resp = await client.get(f"/{encode_doc_id(chunk_id)}")
            if resp.status_code == 200:
                chunk_rev = resp.json().get("_rev")
                del_resp = await client.delete(
                    f"/{encode_doc_id(chunk_id)}",
                    params={"rev": chunk_rev},
                )
                if del_resp.status_code not in (200, 202):
                    failed_chunks.append(chunk_id)
            elif resp.status_code != 404:
                failed_chunks.append(chunk_id)
        if failed_chunks:
            logger.warning(
                "Failed to delete %d chunk(s) for %s: %s",
                len(failed_chunks),
                path,
                failed_chunks[:5],
            )

        # Delete the doc
        doc_rev = doc.get("_rev")
        doc_encoded = encode_doc_id(doc["_id"])
        resp = await client.delete(f"/{doc_encoded}", params={"rev": doc_rev})
        if resp.status_code == 409:
            fresh = await self._get_doc(path)
            if fresh:
                fresh_id = encode_doc_id(fresh["_id"])
                resp = await client.delete(f"/{fresh_id}", params={"rev": fresh["_rev"]})
            else:
                return True  # Already deleted by another client
        resp.raise_for_status()
        return True

    # ── Search ─────────────────────────────────────────────────────

    async def search_notes(
        self, query: str, folder: str | None = None, limit: int = 20
    ) -> list[SearchResult]:
        """Search note content using chunk scanning with reverse map."""
        client = await self._get_client()

        # Build chunk-to-parent reverse map
        all_docs = await self._get_all_file_docs()
        chunk_to_parent: dict[str, dict] = {}
        for doc in all_docs:
            for cid in doc.get("children", []):
                chunk_to_parent[cid] = doc

        # Search chunks using Mango query with regex
        import re

        query_escaped = re.escape(query)

        mango = {
            "selector": {
                "type": "leaf",
                "data": {"$regex": f"(?i){query_escaped}"},
            },
            "fields": ["_id", "data"],
            "limit": 5000,
        }
        resp = await client.post("/_find", json=mango)
        resp.raise_for_status()
        matching_chunks = resp.json().get("docs", [])

        # Group by parent note
        note_matches: dict[str, list[str]] = defaultdict(list)
        for chunk in matching_chunks:
            chunk_id = chunk["_id"]
            parent = chunk_to_parent.get(chunk_id)
            if not parent:
                continue
            parent_path = parent.get("path", parent.get("_id", ""))

            # Filter by folder if specified
            if folder:
                folder_lower = folder.strip("/").lower() + "/"
                if not parent_path.lower().startswith(folder_lower):
                    continue

            # Extract snippet
            data = chunk.get("data", "")
            pattern = re.compile(re.escape(query), re.IGNORECASE)
            match = pattern.search(data)
            if match:
                start = max(0, match.start() - 60)
                end = min(len(data), match.end() + 60)
                snippet = data[start:end].replace("\n", " ").strip()
                if start > 0:
                    snippet = "..." + snippet
                if end < len(data):
                    snippet = snippet + "..."
                note_matches[parent_path].append(snippet)

        # Build results sorted by match count
        results = []
        for path, snippets in note_matches.items():
            results.append(
                SearchResult(
                    path=path,
                    matches=len(snippets),
                    snippets=snippets[:3],  # Cap at 3 snippets per note
                )
            )
        results.sort(key=lambda r: r.matches, reverse=True)
        return results[:limit]

    # ── Frontmatter operations ─────────────────────────────────────

    async def read_frontmatter(self, path: str) -> dict | None:
        """Read and parse frontmatter from a note. Returns None if no frontmatter."""
        note = await self.read_note(path)
        if not note or note.is_binary:
            return None
        fm, _ = extract_frontmatter(note.content)
        return fm

    async def update_frontmatter(self, path: str, properties: dict) -> bool:
        """Merge properties into a note's frontmatter. Creates frontmatter if absent."""
        note = await self.read_note(path)
        if not note:
            raise ValueError(f"Note not found: {path}")
        if note.is_binary:
            raise ValueError(f"Cannot set frontmatter on binary file: {path}")
        new_content = set_frontmatter(note.content, properties)
        return await self.write_note(path, new_content)

    # ── Tag operations ─────────────────────────────────────────────

    async def _read_note_content(self, doc: dict) -> str | None:
        """Read content from a file doc (fetch + reassemble chunks).

        Unlike read_note, logs a warning and returns None on missing chunks
        instead of raising — used in bulk scans (list_tags, get_backlinks)
        where one broken note should not abort the entire operation.
        """
        chunk_ids = doc.get("children", [])
        if not chunk_ids:
            return None
        chunks = await self._fetch_chunks(chunk_ids)
        missing = [cid for cid in chunk_ids if cid not in chunks]
        if missing:
            doc_id = doc.get("_id", "unknown")
            logger.warning("Missing %d chunk(s) for %s: %s", len(missing), doc_id, missing[:3])
            return None
        return "".join(chunks[cid] for cid in chunk_ids)

    async def list_tags(self, folder: str | None = None) -> dict[str, int]:
        """Scan all notes and return tag -> count mapping."""
        all_docs = await self._get_all_file_docs()
        if folder:
            folder_lower = folder.strip("/").lower() + "/"
            all_docs = [
                d for d in all_docs if d.get("_id", "").lstrip("/").startswith(folder_lower)
            ]

        tag_counts: dict[str, int] = defaultdict(int)
        for doc in all_docs:
            if doc.get("type") == "newnote":
                continue
            content = await self._read_note_content(doc)
            if not content:
                continue
            for tag in extract_tags(content):
                tag_counts[tag] += 1

        return dict(sorted(tag_counts.items(), key=lambda x: x[1], reverse=True))

    async def search_by_tag(
        self, tag: str, folder: str | None = None, limit: int = 20
    ) -> list[NoteMetadata]:
        """Find notes containing a specific tag (frontmatter or inline)."""
        all_docs = await self._get_all_file_docs()
        if folder:
            folder_lower = folder.strip("/").lower() + "/"
            all_docs = [
                d for d in all_docs if d.get("_id", "").lstrip("/").startswith(folder_lower)
            ]

        results = []
        tag_lower = tag.lower().lstrip("#")
        for doc in all_docs:
            if doc.get("type") == "newnote":
                continue
            content = await self._read_note_content(doc)
            if not content:
                continue
            note_tags = [t.lower() for t in extract_tags(content)]
            if tag_lower in note_tags:
                results.append(
                    NoteMetadata(
                        path=doc.get("path", doc["_id"]),
                        size=doc.get("size", 0),
                        ctime=doc.get("ctime", 0),
                        mtime=doc.get("mtime", 0),
                        doc_type=doc.get("type", "plain"),
                        chunk_count=len(doc.get("children", [])),
                    )
                )
                if len(results) >= limit:
                    break
        return results

    # ── Link / backlink operations ─────────────────────────────────

    async def get_outbound_links(self, path: str) -> list[str]:
        """Extract wikilink targets from a single note."""
        note = await self.read_note(path)
        if not note or note.is_binary:
            return []
        return extract_wikilinks(note.content)

    async def get_backlinks(self, path: str) -> list[BacklinkInfo]:
        """Find all notes that contain a wikilink pointing to the given path."""
        import re

        # Normalize target: strip folder prefix and extension for matching
        target_name = path.rsplit("/", 1)[-1]  # filename
        if target_name.endswith(".md"):
            target_name = target_name[:-3]
        target_lower = target_name.lower()

        all_docs = await self._get_all_file_docs()
        results = []

        for doc in all_docs:
            doc_path = doc.get("path", doc.get("_id", ""))
            if doc.get("type") == "newnote":
                continue
            content = await self._read_note_content(doc)
            if not content:
                continue

            links = extract_wikilinks(content)
            link_names_lower = [lnk.rsplit("/", 1)[-1].lower() for lnk in links]

            if target_lower in link_names_lower:
                # Extract context snippet around the link
                pattern = re.compile(
                    r"(?:^|\n)([^\n]*\[\[" + re.escape(target_name) + r"[^\]]*\]\][^\n]*)",
                    re.IGNORECASE,
                )
                m = pattern.search(content)
                ctx = m.group(1).strip() if m else ""
                results.append(BacklinkInfo(source_path=doc_path, context=ctx))

        return results
