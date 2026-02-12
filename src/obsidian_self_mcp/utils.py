"""Utility functions for chunk ID generation and path normalization."""

import random
import string
import urllib.parse

_CHUNK_CHARS = string.ascii_lowercase + string.digits


def generate_chunk_id() -> str:
    """Generate a random chunk ID matching LiveSync format: h: + 12 alnum."""
    return "h:" + "".join(random.choice(_CHUNK_CHARS) for _ in range(12))


def normalize_doc_id(vault_path: str) -> str:
    """Convert a vault path to CouchDB doc ID (lowercase).

    CouchDB reserves IDs starting with '_', so paths like '_Changelog/...'
    get a '/' prefix to match Obsidian LiveSync's convention.
    """
    doc_id = vault_path.lstrip("/").lower()
    # CouchDB rejects doc IDs starting with '_' — prefix with '/'
    if doc_id.startswith("_"):
        doc_id = "/" + doc_id
    return doc_id


def encode_doc_id(doc_id: str) -> str:
    """URL-encode a doc ID for CouchDB HTTP requests."""
    return urllib.parse.quote(doc_id, safe="")
