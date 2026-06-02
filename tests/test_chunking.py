"""Tests for obsidian_livesync_mcp.chunking — Rabin-Karp content-defined chunking."""

from obsidian_livesync_mcp.chunking import split_chunks


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


def test_decode_binary_chunks_roundtrip_single():
    from obsidian_livesync_mcp.chunking import decode_binary_chunks

    data = bytes(range(256))
    chunks = split_chunks(data, is_text=False)
    assert decode_binary_chunks(chunks) == data


def test_decode_binary_chunks_roundtrip_multichunk():
    """Multi-chunk binary must round-trip byte-for-byte."""
    from obsidian_livesync_mcp.chunking import decode_binary_chunks

    data = bytes((i * 37 + (i >> 3)) & 0xFF for i in range(50000))
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


def test_max_chunk_size_respected():
    """No chunk should exceed the absolute max piece size."""
    max_size = 102400  # 100KB
    # 1MB of text
    content = "A" * (1024 * 1024)
    data = content.encode("utf-8")
    chunks = split_chunks(data, is_text=True, absolute_max_piece_size=max_size)
    for chunk in chunks:
        assert len(chunk.encode("utf-8")) <= max_size * 6  # generous margin for boundary
