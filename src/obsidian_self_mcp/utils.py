"""Utility functions for chunk ID generation, path normalization, and content parsing."""

import hashlib
import re
import urllib.parse

import xxhash
import yaml


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


def _hash_string(key: str) -> str:
    """SHA-256 hash matching LiveSync's hashString() in path.ts."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def normalize_doc_id(
    vault_path: str,
    obfuscate_passphrase: str | None = None,
    case_insensitive: bool = True,
) -> str:
    """Convert a vault path to CouchDB doc ID, matching LiveSync's path2id_base().

    Args:
        vault_path: The vault-relative file path.
        obfuscate_passphrase: If set, generate an ``f:`` prefixed hash ID
            (LiveSync path obfuscation mode). If None, use the plain path.
        case_insensitive: Lowercase the path before hashing (LiveSync default).
    """
    filename = vault_path.lstrip("/")

    if case_insensitive:
        filename = filename.lower()

    # CouchDB rejects doc IDs starting with '_' — prefix with '/'
    if filename.startswith("_"):
        filename = "/" + filename

    if not obfuscate_passphrase:
        return filename

    # Path obfuscation: f: + SHA-256(SHA-256(passphrase) + ":" + filename)
    hashed_passphrase = _hash_string(obfuscate_passphrase)
    hashed_id = _hash_string(f"{hashed_passphrase}:{filename}")
    return f"f:{hashed_id}"


def encode_doc_id(doc_id: str) -> str:
    """URL-encode a doc ID for CouchDB HTTP requests."""
    return urllib.parse.quote(doc_id, safe="")


# ── Frontmatter parsing ───────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?\r?\n)---\r?\n?", re.DOTALL)


def extract_frontmatter(content: str) -> tuple[dict | None, str]:
    """Parse YAML frontmatter from note content.

    Returns (parsed dict, body without frontmatter).
    Returns (None, original content) if no frontmatter found.
    """
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return None, content
    try:
        data = yaml.safe_load(m.group(1))
    except yaml.YAMLError:
        return None, content
    if not isinstance(data, dict):
        return None, content
    body = content[m.end() :]
    return data, body


def set_frontmatter(content: str, properties: dict) -> str:
    """Merge properties into existing frontmatter (or create it). Preserves body."""
    existing, body = extract_frontmatter(content)
    merged = existing or {}
    merged.update(properties)
    fm_str = yaml.dump(merged, default_flow_style=False, allow_unicode=True).rstrip("\n")
    return f"---\n{fm_str}\n---\n{body}"


# ── Wikilink / tag extraction ─────────────────────────────────────

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]+?)?\]\]")
_INLINE_TAG_RE = re.compile(r"(?:^|(?<=\s))#([A-Za-z][A-Za-z0-9_/-]*)", re.MULTILINE)


def extract_wikilinks(content: str) -> list[str]:
    """Extract wikilink targets from markdown content.

    Handles [[Note]], [[Note|alias]], and [[Note#heading]].
    Returns deduplicated list of link targets (note names).
    """
    seen: set[str] = set()
    result: list[str] = []
    for m in _WIKILINK_RE.finditer(content):
        target = m.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            result.append(target)
    return result


def extract_tags(content: str) -> list[str]:
    """Extract tags from frontmatter (tags field) and inline #tag patterns.

    Returns deduplicated list of tag names (without # prefix).
    """
    fm, body = extract_frontmatter(content)
    seen: set[str] = set()
    result: list[str] = []

    # Frontmatter tags
    if fm:
        fm_tags = fm.get("tags", [])
        if isinstance(fm_tags, str):
            fm_tags = [t.strip() for t in fm_tags.split(",")]
        if isinstance(fm_tags, list):
            for t in fm_tags:
                if not isinstance(t, (str, int)):
                    continue
                tag = str(t).strip().lstrip("#")
                if tag and tag not in seen:
                    seen.add(tag)
                    result.append(tag)

    # Inline tags from body
    for m in _INLINE_TAG_RE.finditer(body):
        tag = m.group(1)
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)

    return result
