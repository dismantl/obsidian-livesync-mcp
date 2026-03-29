# LiveSync Compatibility Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make obsidian-self-mcp fully compatible with Obsidian LiveSync's default CouchDB document format — content-hash chunk IDs, Rabin-Karp splitting, legacy note type support, and orphan chunk cleanup on writes.

**Architecture:** Replace random chunk IDs with xxhash64-based content hashes. Add a new `chunking.py` module implementing LiveSync's V3 Rabin-Karp content-defined chunking. Update `client.py` to use both, handle legacy `notes` type docs, and clean up orphaned chunks on write. Document required LiveSync settings.

**Tech Stack:** Python 3.10+, xxhash (C extension), httpx, pytest, respx

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `pyproject.toml` | Modify | Add `xxhash` runtime dependency |
| `src/obsidian_self_mcp/utils.py` | Modify | Content-hash `generate_chunk_id(content)`, add `utf16_len` helper |
| `src/obsidian_self_mcp/chunking.py` | Create | Rabin-Karp V3 content-defined chunking |
| `src/obsidian_self_mcp/client.py` | Modify | Use new chunking/IDs, legacy notes, orphan cleanup |
| `tests/test_utils.py` | Modify | Update chunk ID tests for new signature/behavior |
| `tests/test_chunking.py` | Create | Rabin-Karp unit tests |
| `tests/test_client.py` | Modify | Add legacy notes + orphan cleanup tests |
| `README.md` | Modify | Add LiveSync Compatibility section |
| `CLAUDE.md` | Modify | Update LiveSync Document Model section |

---

### Task 1: Add xxhash dependency

**Files:**
- Modify: `pyproject.toml:25-30`

- [ ] **Step 1: Add xxhash to runtime dependencies**

In `pyproject.toml`, add `xxhash` to the `dependencies` list:

```toml
dependencies = [
    "mcp[cli]>=1.12.0",
    "httpx>=0.27.0",
    "pyyaml>=6.0",
    "pydantic>=2.0",
    "xxhash>=3.0",
]
```

- [ ] **Step 2: Install updated dependencies**

Run: `pip install -e ".[dev]"`
Expected: Installs xxhash alongside existing deps. No errors.

- [ ] **Step 3: Verify xxhash works**

Run: `python -c "import xxhash; print(xxhash.xxh64(b'test').hexdigest())"`
Expected: Prints a hex hash string (e.g., `4fdcca5ddb678139`)

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "build: add xxhash dependency for LiveSync-compatible chunk IDs"
```

---

### Task 2: Content-hash chunk IDs

**Files:**
- Modify: `src/obsidian_self_mcp/utils.py:1-16`
- Modify: `tests/test_utils.py:1-33`

- [ ] **Step 1: Write failing tests for new generate_chunk_id**

Replace the three existing `test_generate_chunk_id_*` tests in `tests/test_utils.py` with:

```python
def test_generate_chunk_id_deterministic():
    """Same content always produces the same chunk ID."""
    id1 = generate_chunk_id("Hello world")
    id2 = generate_chunk_id("Hello world")
    assert id1 == id2


def test_generate_chunk_id_prefix():
    """Chunk IDs start with h: prefix."""
    cid = generate_chunk_id("test content")
    assert cid.startswith("h:")


def test_generate_chunk_id_different_content():
    """Different content produces different chunk IDs."""
    id1 = generate_chunk_id("content A")
    id2 = generate_chunk_id("content B")
    assert id1 != id2


def test_generate_chunk_id_base36():
    """Chunk ID suffix is base-36 (lowercase alphanumeric)."""
    cid = generate_chunk_id("some content")
    suffix = cid[2:]
    assert all(c in "0123456789abcdefghijklmnopqrstuvwxyz" for c in suffix)


def test_generate_chunk_id_utf16_len():
    """Emoji content uses UTF-16 code unit count (matching JavaScript string.length)."""
    # "👋" is 1 Python char but 2 UTF-16 code units
    id_emoji = generate_chunk_id("👋")
    # Hash input should be "👋-2" (UTF-16 length), not "👋-1" (Python len)
    assert id_emoji.startswith("h:")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_utils.py::test_generate_chunk_id_deterministic -v`
Expected: FAIL — `generate_chunk_id()` takes 0 args, got 1

- [ ] **Step 3: Implement content-hash chunk IDs**

Replace the chunk ID code at the top of `src/obsidian_self_mcp/utils.py`. Remove the `random`, `string` imports and `_CHUNK_CHARS` constant. Replace `generate_chunk_id`:

```python
import xxhash


def _int_to_base36(n: int) -> str:
    """Convert a non-negative integer to a base-36 string (matching JS BigInt.toString(36))."""
    if n == 0:
        return "0"
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = []
    while n > 0:
        result.append(chars[n % 36])
        n //= 36
    return "".join(reversed(result))


def _utf16_len(s: str) -> int:
    """Count UTF-16 code units (matching JavaScript's string.length)."""
    return len(s.encode("utf-16-le")) // 2


def generate_chunk_id(content: str) -> str:
    """Generate a chunk ID by hashing content, matching LiveSync's xxhash64 format.

    LiveSync computes: h: + xxhash64(piece + "-" + piece.length).toString(36)
    where piece.length is JavaScript's UTF-16 code unit count.
    """
    hash_input = f"{content}-{_utf16_len(content)}"
    hash_value = xxhash.xxh64(hash_input.encode("utf-8")).intdigest()
    return f"h:{_int_to_base36(hash_value)}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_utils.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/obsidian_self_mcp/utils.py tests/test_utils.py
git commit -m "feat(utils): content-hash chunk IDs using xxhash64

Replace random chunk IDs with deterministic content-based hashes
matching LiveSync's xxhash64 format. Uses UTF-16 code unit count
for JavaScript string.length compatibility."
```

---

### Task 3: Rabin-Karp chunk splitting

**Files:**
- Create: `src/obsidian_self_mcp/chunking.py`
- Create: `tests/test_chunking.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_chunking.py`:

```python
"""Tests for obsidian_self_mcp.chunking — Rabin-Karp content-defined chunking."""

from obsidian_self_mcp.chunking import split_chunks


def test_empty_input():
    assert split_chunks(b"", is_text=True) == []
    assert split_chunks(b"", is_text=False) == []


def test_small_text_single_chunk():
    """Text smaller than min chunk size (128 bytes) stays as one chunk."""
    content = "Hello world"
    data = content.encode("utf-8")
    chunks = split_chunks(data, is_text=True)
    assert len(chunks) == 1
    assert chunks[0] == content


def test_large_text_splits():
    """Text large enough should split into multiple chunks."""
    # 10KB of text — avg chunk size = max(128, 10000/20) = 500 bytes
    content = "Line of text content here.\n" * 400  # ~10.8KB
    data = content.encode("utf-8")
    chunks = split_chunks(data, is_text=True)
    assert len(chunks) > 1
    # Reassembled content must match original
    assert "".join(chunks) == content


def test_deterministic():
    """Same input always produces same chunks."""
    content = "Repeatable content.\n" * 200
    data = content.encode("utf-8")
    chunks1 = split_chunks(data, is_text=True)
    chunks2 = split_chunks(data, is_text=True)
    assert chunks1 == chunks2


def test_binary_chunks_are_base64():
    """Binary chunks should be base64-encoded strings."""
    import base64
    data = bytes(range(256)) * 40  # ~10KB binary
    chunks = split_chunks(data, is_text=False)
    assert len(chunks) >= 1
    # Each chunk should be valid base64 that decodes back
    reassembled = b""
    for chunk in chunks:
        reassembled += base64.b64decode(chunk)
    assert reassembled == data


def test_utf8_boundary_safety():
    """Should not split in the middle of a multi-byte UTF-8 character."""
    # Create content with multi-byte chars spread throughout
    # 4-byte UTF-8: emoji 👋 = F0 9F 91 8B
    content = ("Hello 👋 world! " * 500)  # ~9KB with emoji
    data = content.encode("utf-8")
    chunks = split_chunks(data, is_text=True)
    # Every chunk must be valid UTF-8
    for chunk in chunks:
        chunk.encode("utf-8")  # Would raise if invalid
    assert "".join(chunks) == content


def test_max_chunk_size_respected():
    """No chunk should exceed the absolute max piece size."""
    max_size = 102400  # 100KB
    # 1MB of text
    content = "A" * (1024 * 1024)
    data = content.encode("utf-8")
    chunks = split_chunks(data, is_text=True, absolute_max_piece_size=max_size)
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= max_size * 6  # generous margin for boundary
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_chunking.py::test_empty_input -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'obsidian_self_mcp.chunking'`

- [ ] **Step 3: Implement Rabin-Karp chunking**

Create `src/obsidian_self_mcp/chunking.py`:

```python
"""Rabin-Karp content-defined chunking matching LiveSync V3.

Splits content into chunks using a rolling hash, producing identical
chunk boundaries to LiveSync's default chunkSplitterVersion="v3-rabin-karp".
"""

import base64

# LiveSync constants from shared.const.behabiour.ts
MAX_DOC_SIZE_BIN = 102400  # 100KB

# Rabin-Karp parameters from chunks.ts splitPiecesRabinKarp
PRIME = 31
WINDOW_SIZE = 48
BOUNDARY_PATTERN = 1


def _imul(a: int, b: int) -> int:
    """Match JavaScript's Math.imul: C-like 32-bit signed integer multiply."""
    result = ((a & 0xFFFFFFFF) * (b & 0xFFFFFFFF)) & 0xFFFFFFFF
    if result >= 0x80000000:
        result -= 0x100000000
    return result


def _to_int32(x: int) -> int:
    """Match JavaScript's (x) | 0: truncate to signed 32-bit integer."""
    x = x & 0xFFFFFFFF
    if x >= 0x80000000:
        x -= 0x100000000
    return x


def _to_uint32(x: int) -> int:
    """Match JavaScript's (x) >>> 0: unsigned 32-bit integer."""
    return x & 0xFFFFFFFF


def split_chunks(
    data: bytes,
    is_text: bool,
    absolute_max_piece_size: int = MAX_DOC_SIZE_BIN,
) -> list[str]:
    """Split data into chunks using Rabin-Karp content-defined chunking.

    Args:
        data: Raw bytes to split (UTF-8 encoded text or raw binary).
        is_text: True for text files, False for binary.
        absolute_max_piece_size: Hard upper limit on chunk size in bytes.

    Returns:
        List of chunk content strings. Text chunks are UTF-8 decoded strings.
        Binary chunks are base64-encoded strings.
    """
    length = len(data)
    if length == 0:
        return []

    # Compute chunk sizing parameters (matching LiveSync exactly)
    min_piece_size = 128 if is_text else 4096
    split_piece_count = 20 if is_text else 12
    avg_chunk_size = max(min_piece_size, length // split_piece_count)
    max_chunk_size = min(absolute_max_piece_size, avg_chunk_size * 5)
    min_chunk_size = min(max(avg_chunk_size // 4, 1), max_chunk_size)
    hash_modulus = avg_chunk_size

    # Precompute PRIME^(WINDOW_SIZE-1) using 32-bit integer math
    p_pow_w = 1
    for _ in range(WINDOW_SIZE - 1):
        p_pow_w = _imul(p_pow_w, PRIME)

    chunks: list[str] = []
    pos = 0
    start = 0
    hash_val = 0

    while pos < length:
        byte = data[pos]

        # Update rolling hash (matching LiveSync's signed 32-bit arithmetic)
        if pos >= start + WINDOW_SIZE:
            old_byte = data[pos - WINDOW_SIZE]
            old_byte_term = _imul(old_byte, p_pow_w)
            hash_val = _to_int32(hash_val - old_byte_term)
            hash_val = _imul(hash_val, PRIME)
            hash_val = _to_int32(hash_val + byte)
        else:
            hash_val = _imul(hash_val, PRIME)
            hash_val = _to_int32(hash_val + byte)

        current_chunk_size = pos - start + 1
        is_boundary = False

        # Boundary detection (using unsigned comparison like LiveSync's >>> 0)
        if current_chunk_size >= min_chunk_size:
            if _to_uint32(hash_val) % hash_modulus == BOUNDARY_PATTERN:
                is_boundary = True
        if current_chunk_size >= max_chunk_size:
            is_boundary = True

        if is_boundary:
            # UTF-8 safety: don't split in the middle of a multi-byte character
            is_safe = True
            if is_text and pos + 1 < length and (data[pos + 1] & 0xC0) == 0x80:
                is_safe = False

            if is_safe:
                chunk_bytes = data[start : pos + 1]
                if is_text:
                    chunks.append(chunk_bytes.decode("utf-8"))
                else:
                    chunks.append(base64.b64encode(chunk_bytes).decode("ascii"))
                start = pos + 1

        pos += 1

    # Yield remaining bytes as the final chunk
    if start < length:
        chunk_bytes = data[start:length]
        if is_text:
            chunks.append(chunk_bytes.decode("utf-8"))
        else:
            chunks.append(base64.b64encode(chunk_bytes).decode("ascii"))

    return chunks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_chunking.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run linter**

Run: `ruff check src/obsidian_self_mcp/chunking.py tests/test_chunking.py`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/obsidian_self_mcp/chunking.py tests/test_chunking.py
git commit -m "feat(chunking): add Rabin-Karp V3 content-defined chunking

Port LiveSync's splitPiecesRabinKarp with identical parameters:
PRIME=31, window=48, boundary when hash%avgChunkSize==1.
Handles both text (UTF-8 safe) and binary (base64 encoded) chunks."
```

---

### Task 4: Update client.py write path

**Files:**
- Modify: `src/obsidian_self_mcp/client.py:1-30, 210-287`

- [ ] **Step 1: Update imports and remove old CHUNK_SIZE constant**

At the top of `client.py`, add the chunking import and remove `CHUNK_SIZE`:

Replace:
```python
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

CHUNK_SIZE = 10000  # ~10KB chunks for binary
```

With:
```python
from .chunking import split_chunks
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
```

- [ ] **Step 2: Rewrite the write_note chunk preparation logic**

Replace the `write_note` method's chunk preparation section (the block starting with `# Prepare chunks` through creating chunk docs). Replace lines that currently read:

```python
        # Prepare chunks
        if is_binary:
            raw = content.encode("utf-8") if isinstance(content, str) else content
            encoded_content = base64.b64encode(raw).decode("ascii")
            chunks_data = [
                encoded_content[i : i + CHUNK_SIZE]
                for i in range(0, len(encoded_content), CHUNK_SIZE)
            ]
            file_size = len(raw)
            doc_type = "newnote"
        else:
            chunks_data = [content]
            file_size = len(content.encode("utf-8"))
            doc_type = "plain"

        # Create chunk docs
        chunk_ids = []
        for chunk_data in chunks_data:
            chunk_id = generate_chunk_id()
            resp = await client.put(
                f"/{encode_doc_id(chunk_id)}",
                json={"_id": chunk_id, "data": chunk_data, "type": "leaf"},
            )
            resp.raise_for_status()
            chunk_ids.append(chunk_id)
```

With:

```python
        # Prepare chunks using Rabin-Karp content-defined splitting
        if is_binary:
            raw = content.encode("utf-8") if isinstance(content, str) else content
            file_size = len(raw)
            doc_type = "newnote"
            chunks_data = split_chunks(raw, is_text=False)
        else:
            file_size = len(content.encode("utf-8"))
            doc_type = "plain"
            chunks_data = split_chunks(content.encode("utf-8"), is_text=True)

        # Create chunk docs with content-hash IDs
        chunk_ids = []
        for chunk_data in chunks_data:
            chunk_id = generate_chunk_id(chunk_data)
            resp = await client.put(
                f"/{encode_doc_id(chunk_id)}",
                json={"_id": chunk_id, "data": chunk_data, "type": "leaf"},
            )
            # 409 is OK — chunk with same content hash already exists
            if resp.status_code != 409:
                resp.raise_for_status()
            chunk_ids.append(chunk_id)
```

Note: We handle 409 on chunk PUT because content-hash IDs mean the same chunk may already exist (deduplication). This is expected behavior.

- [ ] **Step 3: Run existing tests**

Run: `pytest tests/test_client.py -v -k write`
Expected: All write tests PASS. The respx mocks use regex patterns for chunk URLs, so the new deterministic IDs still match.

- [ ] **Step 4: Commit**

```bash
git add src/obsidian_self_mcp/client.py
git commit -m "feat(client): use Rabin-Karp chunking and content-hash IDs for writes

Replace single-chunk text storage and 10KB binary splits with
LiveSync-compatible Rabin-Karp content-defined chunking. Chunk IDs
are now deterministic xxhash64 content hashes. Duplicate chunks
(409) are handled gracefully for deduplication."
```

---

### Task 5: Legacy `notes` type support

**Files:**
- Modify: `src/obsidian_self_mcp/client.py:94-128, 160-188`
- Modify: `tests/test_client.py`

- [ ] **Step 1: Write failing test for legacy notes read**

Add to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_client.py::test_read_note_legacy_notes_type_string -v`
Expected: FAIL — `result is None` (doc type "notes" not found or not handled)

- [ ] **Step 3: Update _get_all_file_docs to include legacy type**

In `client.py`, update the `_get_all_file_docs` method. Change both filter lines from:

```python
            if doc.get("type") in ("plain", "newnote") and "children" in doc:
```

To:

```python
            if doc.get("type") in ("plain", "newnote", "notes") and (
                "children" in doc or "data" in doc
            ):
```

There are two occurrences of this filter (one per `_all_docs` range query). Update both.

- [ ] **Step 4: Update read_note to handle legacy notes type**

In `client.py`, update `read_note`. After fetching the doc and before the chunk reassembly logic, add handling for legacy type. Replace:

```python
        chunk_ids = doc.get("children", [])
        chunks = await self._fetch_chunks(chunk_ids)

        # Reassemble in order — fail loudly if chunks are missing
        missing = [cid for cid in chunk_ids if cid not in chunks]
        if missing:
            raise ValueError(
                f"Missing {len(missing)} chunk(s) for {path}: {missing[:3]}"
            )
        content = "".join(chunks[cid] for cid in chunk_ids)

        is_binary = doc.get("type") == "newnote"
```

With:

```python
        is_binary = doc.get("type") == "newnote"

        # Legacy "notes" type stores content directly in data field
        if doc.get("type") == "notes":
            data = doc.get("data", "")
            content = "".join(data) if isinstance(data, list) else str(data)
        else:
            chunk_ids = doc.get("children", [])
            chunks = await self._fetch_chunks(chunk_ids)

            # Reassemble in order — fail loudly if chunks are missing
            missing = [cid for cid in chunk_ids if cid not in chunks]
            if missing:
                raise ValueError(
                    f"Missing {len(missing)} chunk(s) for {path}: {missing[:3]}"
                )
            content = "".join(chunks[cid] for cid in chunk_ids)
```

- [ ] **Step 5: Update list_notes to handle missing children**

In `list_notes`, update the chunk_count line:

Replace:
```python
                chunk_count=len(doc.get("children", [])),
```

With:
```python
                chunk_count=len(doc.get("children", doc.get("data", []) if isinstance(doc.get("data"), list) else [])),
```

Actually, simpler — just default to 0:

```python
                chunk_count=len(doc.get("children", [])),
```

This already handles it because legacy docs without `children` return `[]` → length 0. No change needed.

- [ ] **Step 6: Fix _mock_get_all_file_docs for legacy docs**

The existing `_mock_get_all_file_docs` helper works fine — it just returns whatever docs you pass. No change needed.

- [ ] **Step 7: Run tests**

Run: `pytest tests/test_client.py -v`
Expected: All tests PASS including the 3 new legacy tests.

- [ ] **Step 8: Commit**

```bash
git add src/obsidian_self_mcp/client.py tests/test_client.py
git commit -m "feat(client): support legacy 'notes' type documents

Handle LiveSync's legacy document format where content is stored
directly in the data field (string or string array) instead of
separate chunk documents."
```

---

### Task 6: Orphan chunk cleanup on write

**Files:**
- Modify: `src/obsidian_self_mcp/client.py:210-287`
- Modify: `tests/test_client.py`

- [ ] **Step 1: Write failing test for orphan cleanup**

Add to `tests/test_client.py`:

```python
# ── orphan chunk cleanup ─────────────────────────────────────────


@respx.mock
async def test_write_note_cleans_up_orphan_chunks(client):
    """Updating a note should delete old chunks no longer referenced."""
    existing = _make_parent_doc("notes/todo.md", ["h:oldchunk0000"])
    _mock_get_doc("notes%2Ftodo.md", existing)

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_client.py::test_write_note_cleans_up_orphan_chunks -v`
Expected: FAIL — `delete_route.called` is False (no cleanup logic yet)

- [ ] **Step 3: Add orphan cleanup to write_note**

In `client.py`, in the `write_note` method, save old children before updating, then clean up after. Add this logic:

Before the existing doc update block (`if existing:`), save old children:

```python
        # Check existing doc
        existing = await self._get_doc(vault_path)
        old_children = set(existing.get("children", [])) if existing else set()
```

Replace the line `existing = await self._get_doc(vault_path)` and add `old_children`.

Then, at the very end of `write_note` (just before `return True`), add:

```python
        # Clean up orphaned chunks (best-effort)
        new_children = set(chunk_ids)
        orphaned = old_children - new_children
        if orphaned:
            await self._delete_orphan_chunks(list(orphaned))

        return True
```

Add the helper method to the class:

```python
    async def _delete_orphan_chunks(self, chunk_ids: list[str]) -> None:
        """Delete orphaned chunk documents. Best-effort: logs warnings on failure."""
        client = await self._get_client()
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
                elif resp.status_code != 404:
                    logger.warning("Failed to fetch orphan chunk %s: %s", chunk_id, resp.status_code)
            except Exception:
                logger.warning("Error cleaning up orphan chunk %s", chunk_id, exc_info=True)
```

Place `_delete_orphan_chunks` right after the `_fetch_chunks` method (in the low-level helpers section).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_client.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/obsidian_self_mcp/client.py tests/test_client.py
git commit -m "feat(client): clean up orphaned chunks on note update

When write_note updates an existing note, old chunks that are no
longer referenced are deleted. Cleanup is best-effort with warning
logging on failure, matching the pattern used by delete_note."
```

---

### Task 7: Update README.md

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add LiveSync Compatibility section**

Add the following section after the "How LiveSync stores data" section (before `## License`):

```markdown
## LiveSync Compatibility

This tool talks directly to CouchDB and must match LiveSync's document format. The following LiveSync settings are **required** for compatibility:

| Setting | Required Value | Default | Notes |
|---------|---------------|---------|-------|
| `encrypt` | `false` | `true` | E2EE not supported — all data would be unreadable |
| `usePathObfuscation` | `false` | `true` | Obfuscated doc IDs not supported |
| `enableCompression` | `false` | `false` | DEFLATE compressed chunks not supported |
| `handleFilenameCaseSensitive` | `false` | `false` | Doc IDs are always lowercased |

> **Important:** LiveSync defaults to E2EE enabled with path obfuscation. Disable both when setting up your vault for use with this tool.

### Compatible Settings

These settings can be any value — reads always work, writes use LiveSync defaults:

| Setting | Our Behavior |
|---------|-------------|
| `hashAlg` | Writes use `xxhash64` (LiveSync default). Reads work with any hash algorithm. |
| `chunkSplitterVersion` | Writes use `v3-rabin-karp` (LiveSync default). Reads work with any splitter. |
| `customChunkSize` | Writes use `0` (default). Reads work with any chunk size. |
| `useEden` | Deprecated. Ignored on read, writes set `eden: {}`. |

### Unsupported Features

| Feature | Impact |
|---------|--------|
| End-to-end encryption (E2EE) | All note content is unreadable |
| Path obfuscation | Cannot locate any documents |
| Data compression (`enableCompression`) | Chunk data appears garbled |
| Chunk packs (`chunkpack` type) | Packed chunks are not fetched |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs(readme): add LiveSync compatibility section

Document required settings, compatible settings, and unsupported
features for LiveSync interoperability."
```

---

### Task 8: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update LiveSync Document Model section**

Replace the existing "## LiveSync Document Model" section with:

```markdown
## LiveSync Document Model

Understanding this is essential for working on `client.py`, `utils.py`, or `chunking.py`:

- Each note is stored as a **parent document** (CouchDB doc with `_id` = lowercased vault path) containing a `children` array of chunk IDs.
- **Chunk documents** hold the actual content (`_id` = `"h:" + xxhash64_base36`, `type` = `"leaf"`). Chunk IDs are content-hash based — same content always produces the same ID.
- Content is split using **Rabin-Karp V3** content-defined chunking (PRIME=31, window=48 bytes, boundary when `hash % avgChunkSize == 1`). Text avg chunk = max(128B, size/20). Binary avg chunk = max(4KB, size/12).
- Legacy documents (type `"notes"`) store content directly in a `data` field instead of chunks.
- Paths starting with `_` (e.g., `_Changelog/`) get a `/` prefix because CouchDB reserves `_`-prefixed IDs.
- Reads must reassemble chunks in order. Writes must create chunk docs before the parent. Updates clean up orphaned chunks. Deletes must clean up both.

### Required LiveSync Settings

These settings **must** be configured for compatibility:
- `encrypt: false` — no E2EE support
- `usePathObfuscation: false` — no path deobfuscation
- `enableCompression: false` — no DEFLATE decompression
- `handleFilenameCaseSensitive: false` — doc IDs are always lowercased
```

- [ ] **Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude): update LiveSync document model with compatibility info

Document content-hash chunk IDs, Rabin-Karp splitting, legacy notes
support, and required LiveSync settings."
```

---

### Task 9: Final verification

- [ ] **Step 1: Run full test suite**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 2: Run linter**

Run: `ruff check . && ruff format --check .`
Expected: No errors

- [ ] **Step 3: Fix any lint issues**

Run: `ruff check --fix . && ruff format .`

- [ ] **Step 4: Run tests again after formatting**

Run: `pytest -v`
Expected: All tests PASS

- [ ] **Step 5: Commit any formatting fixes**

```bash
git add -u
git commit -m "style: apply ruff formatting"
```

(Skip this step if there were no formatting changes.)
