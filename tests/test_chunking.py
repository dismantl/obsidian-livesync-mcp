"""Tests for obsidian_livesync_mcp.chunking — Rabin-Karp content-defined chunking."""

import json
from pathlib import Path

from obsidian_livesync_mcp.chunking import split_chunks
from obsidian_livesync_mcp.utils import generate_chunk_id

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name):
    meta = json.loads((FIXTURES / f"{name}.json").read_text())
    data = (FIXTURES / meta["data_file"]).read_bytes()
    return data, meta


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
    """Text large enough should split into multiple chunks.

    Fixed-size v3 sizing targets 256 B average / 1 KiB max for this text, so
    it splits more aggressively than the old size/20 model.
    """
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


def test_decode_binary_chunks_roundtrip_single():
    from obsidian_livesync_mcp.chunking import decode_binary_chunks

    data = bytes(range(256))
    chunks = split_chunks(data, is_text=False)
    assert decode_binary_chunks(chunks) == data


def test_decode_binary_chunks_roundtrip_multichunk():
    """Multi-chunk binary must round-trip byte-for-byte."""
    from obsidian_livesync_mcp.chunking import decode_binary_chunks

    data = bytes((i * 37 + (i >> 3)) & 0xFF for i in range(250000))
    chunks = split_chunks(data, is_text=False)
    assert len(chunks) > 1
    assert decode_binary_chunks(chunks) == data


def test_decode_binary_chunks_empty():
    from obsidian_livesync_mcp.chunking import decode_binary_chunks

    assert decode_binary_chunks([]) == b""


def test_utf8_boundary_safety():
    """Should not split in the middle of a multi-byte UTF-8 character."""
    # 4-byte UTF-8: emoji 👋 = F0 9F 91 8B
    content = "Hello 👋 world! " * 500  # ~9KB with emoji
    data = content.encode("utf-8")
    chunks = split_chunks(data, is_text=True)
    # Every chunk must be valid UTF-8
    for chunk in chunks:
        chunk.encode("utf-8")  # Would raise if invalid
    assert "".join(chunks) == content


def test_text_chunk_boundary_preserves_leading_bom():
    """Regression guard for LiveSync 0.25.81's U+FEFF chunk-boundary fix."""
    content = f"{'a' * 1024}\ufeff{'b' * 1024}"
    chunks = split_chunks(content.encode("utf-8"), is_text=True)

    assert len(chunks) == 3
    assert chunks[1].startswith("\ufeff")
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


def test_text_fixture_matches_livesync_v3_ids():
    """Tracks LiveSync v3-rabin-karp chunk IDs as of commonlib 6abcea69."""
    data, meta = _load_fixture("multi_boundary_text")
    ids = [generate_chunk_id(chunk) for chunk in split_chunks(data, is_text=True)]
    assert ids == meta["expected_chunk_ids"]
    assert len(ids) == meta["expected_count"]


def test_binary_fixture_matches_livesync_v3_ids():
    """Tracks binary chunk IDs from the same LiveSync v3 sizing model."""
    data, meta = _load_fixture("multi_boundary_binary")
    ids = [generate_chunk_id(chunk) for chunk in split_chunks(data, is_text=False)]
    assert ids == meta["expected_chunk_ids"]
    assert len(ids) == meta["expected_count"]
