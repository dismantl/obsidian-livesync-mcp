"""Utility functions for chunk ID generation, path normalization, and content parsing."""

import random
import re
import string
import urllib.parse

import yaml

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
    body = content[m.end():]
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
