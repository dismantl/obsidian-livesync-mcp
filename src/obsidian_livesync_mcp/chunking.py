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


def decode_binary_chunks(chunks: list[str]) -> bytes:
    """Reassemble binary file bytes from base64-encoded chunks.

    LiveSync base64-encodes each binary byte range independently. Decode each
    chunk first, then concatenate bytes; concatenating base64 strings corrupts
    multi-chunk files when a chunk length is not a multiple of 3.
    """
    return b"".join(base64.b64decode(chunk) for chunk in chunks)
