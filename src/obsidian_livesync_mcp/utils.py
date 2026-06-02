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
_ATTACHMENT_WIKILINK_RE = re.compile(r"(?P<bang>!)?\[\[(?P<body>[^\]]+?)\]\]")


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


def _is_vault_ref(target: str) -> bool:
    target = target.strip()
    if not target or target.startswith("#"):
        return False
    parsed = urllib.parse.urlsplit(target)
    return not parsed.scheme and not parsed.netloc


def _find_markdown_target_close(content: str, open_paren: int) -> int:
    depth = 1
    in_angle_target = False
    pos = open_paren + 1
    while pos < len(content):
        char = content[pos]
        if char == "\\":
            pos += 2
            continue
        if in_angle_target:
            if char == ">":
                in_angle_target = False
        elif char == "<":
            in_angle_target = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return pos
        pos += 1
    return -1


def _iter_markdown_refs(content: str):
    pos = 0
    while True:
        label_start = content.find("[", pos)
        if label_start == -1:
            return
        label_end = content.find("]", label_start + 1)
        if label_end == -1:
            return
        open_paren = label_end + 1
        if open_paren >= len(content) or content[open_paren] != "(":
            pos = label_start + 1
            continue
        close_paren = _find_markdown_target_close(content, open_paren)
        if close_paren == -1:
            pos = label_start + 1
            continue

        has_bang = label_start > 0 and content[label_start - 1] == "!"
        start = label_start - 1 if has_bang else label_start
        yield {
            "start": start,
            "end": close_paren + 1,
            "bang": "!" if has_bang else "",
            "label": content[label_start + 1 : label_end],
            "target": content[open_paren + 1 : close_paren],
        }
        pos = close_paren + 1


def extract_attachment_refs(content: str) -> list[str]:
    """Extract attachment-style references from wikilinks and Markdown links."""
    seen: set[str] = set()
    result: list[str] = []
    for match in _ATTACHMENT_WIKILINK_RE.finditer(content):
        target = match.group("body").strip()
        if target and target not in seen:
            seen.add(target)
            result.append(target)
    for match in _iter_markdown_refs(content):
        target = match["target"].strip()
        split_target, _, _ = _split_markdown_ref(target)
        if _is_vault_ref(split_target) and target and target not in seen:
            seen.add(target)
            result.append(target)
    return result


def _split_markdown_ref(ref: str) -> tuple[str, str, bool]:
    ref = ref.strip()
    if ref.startswith("<"):
        end = ref.find(">")
        if end != -1:
            return ref[1:end], ref[end + 1 :], True
    parts = ref.split(maxsplit=1)
    if not parts:
        return "", "", False
    if len(parts) == 1:
        return parts[0], "", False
    suffix_candidate = parts[1].lstrip()
    if suffix_candidate.startswith(('"', "'")) or (
        suffix_candidate.startswith("(") and suffix_candidate.endswith(")")
    ):
        return parts[0], f" {parts[1]}", False
    return ref, "", False


def _split_wikilink_ref(ref: str) -> tuple[str, str]:
    first_suffix = len(ref)
    for marker in ("#", "|"):
        pos = ref.find(marker)
        if pos != -1:
            first_suffix = min(first_suffix, pos)
    return ref[:first_suffix], ref[first_suffix:]


def _split_target_fragment(target: str) -> tuple[str, str]:
    hash_pos = target.find("#")
    if hash_pos == -1:
        return target, ""
    return target[:hash_pos], target[hash_pos:]


def ref_basename(ref: str) -> str:
    """Return a case-folded basename for a link target."""
    target, _, _ = _split_markdown_ref(ref)
    target, _ = _split_wikilink_ref(target)
    target, _ = _split_target_fragment(target)
    target = urllib.parse.unquote(target.strip())
    return target.rsplit("/", 1)[-1].lower()


def _replacement_path(original_target: str, new_path: str) -> str:
    target = urllib.parse.unquote(original_target.strip().strip("<>"))
    target, fragment = _split_target_fragment(target)
    if "/" in target:
        return new_path.lstrip("/") + fragment
    return new_path.rsplit("/", 1)[-1] + fragment


def rewrite_attachment_refs(content: str, old_path: str, new_path: str) -> tuple[str, int]:
    """Rewrite references matching an attachment basename.

    Matching is intentionally basename-only and case-insensitive, mirroring the
    existing backlink behavior. Folder-duplicate attachment names are ambiguous.
    """
    old_base = ref_basename(old_path)
    count = 0

    def replace_wikilink(match: re.Match[str]) -> str:
        nonlocal count
        body = match.group("body")
        target, suffix = _split_wikilink_ref(body)
        if ref_basename(target) != old_base:
            return match.group(0)
        count += 1
        bang = match.group("bang") or ""
        return f"{bang}[[{_replacement_path(target, new_path)}{suffix}]]"

    rewritten = _ATTACHMENT_WIKILINK_RE.sub(replace_wikilink, content)

    cursor = 0
    markdown_parts: list[str] = []
    for match in _iter_markdown_refs(rewritten):
        raw_target = match["target"]
        target, suffix, angled = _split_markdown_ref(raw_target)
        if not _is_vault_ref(target) or ref_basename(target) != old_base:
            continue
        count += 1
        replacement = _replacement_path(target, new_path)
        if angled or " " in replacement:
            replacement = f"<{replacement}>"
        markdown_parts.append(rewritten[cursor : match["start"]])
        markdown_parts.append(f"{match['bang']}[{match['label']}]({replacement}{suffix})")
        cursor = match["end"]
    if markdown_parts:
        markdown_parts.append(rewritten[cursor:])
        rewritten = "".join(markdown_parts)
    return rewritten, count


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
