"""Tests for obsidian_self_mcp.utils — pure function tests."""

from obsidian_self_mcp.utils import (
    encode_doc_id,
    extract_frontmatter,
    extract_tags,
    extract_wikilinks,
    generate_chunk_id,
    normalize_doc_id,
    set_frontmatter,
)

# ── generate_chunk_id ─────────────────────────────────────────────


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


# ── normalize_doc_id ──────────────────────────────────────────────


def test_normalize_doc_id_basic():
    assert normalize_doc_id("Notes/todo.md") == "notes/todo.md"


def test_normalize_doc_id_uppercase():
    assert normalize_doc_id("Dev Projects/README.md") == "dev projects/readme.md"


def test_normalize_doc_id_underscore_prefix():
    """CouchDB reserves _ prefix — LiveSync prepends /."""
    assert normalize_doc_id("_Changelog/entry.md") == "/_changelog/entry.md"


def test_normalize_doc_id_strips_leading_slash():
    assert normalize_doc_id("/Notes/todo.md") == "notes/todo.md"


def test_normalize_doc_id_empty():
    assert normalize_doc_id("") == ""


# ── encode_doc_id ─────────────────────────────────────────────────


def test_encode_doc_id_slashes():
    assert encode_doc_id("notes/todo.md") == "notes%2Ftodo.md"


def test_encode_doc_id_underscore_prefix():
    assert encode_doc_id("/_changelog/entry.md") == "%2F_changelog%2Fentry.md"


def test_encode_doc_id_spaces():
    assert encode_doc_id("dev projects/readme.md") == "dev%20projects%2Freadme.md"


# ── extract_frontmatter ──────────────────────────────────────────


def test_extract_frontmatter_basic():
    content = "---\ntitle: Hello\ntags: [a, b]\n---\nBody text"
    fm, body = extract_frontmatter(content)
    assert fm == {"title": "Hello", "tags": ["a", "b"]}
    assert body == "Body text"


def test_extract_frontmatter_none():
    content = "No frontmatter here"
    fm, body = extract_frontmatter(content)
    assert fm is None
    assert body == content


def test_extract_frontmatter_empty_yaml():
    content = "---\n---\nBody"
    fm, body = extract_frontmatter(content)
    # yaml.safe_load on empty string returns None, not a dict
    assert fm is None
    assert body == content


def test_extract_frontmatter_malformed_yaml():
    content = "---\n: invalid: yaml: [[\n---\nBody"
    fm, body = extract_frontmatter(content)
    assert fm is None
    assert body == content


def test_extract_frontmatter_non_dict_yaml():
    """YAML that parses to a list/string should return None."""
    content = "---\n- item1\n- item2\n---\nBody"
    fm, body = extract_frontmatter(content)
    assert fm is None
    assert body == content


def test_extract_frontmatter_crlf():
    content = "---\r\ntitle: Hello\r\n---\r\nBody"
    fm, body = extract_frontmatter(content)
    assert fm == {"title": "Hello"}
    assert body == "Body"


# ── set_frontmatter ──────────────────────────────────────────────


def test_set_frontmatter_create():
    content = "Body text"
    result = set_frontmatter(content, {"status": "done"})
    assert result.startswith("---\n")
    assert "status: done" in result
    assert result.endswith("---\nBody text")


def test_set_frontmatter_merge():
    content = "---\ntitle: Hello\n---\nBody"
    result = set_frontmatter(content, {"status": "done"})
    fm, body = extract_frontmatter(result)
    assert fm["title"] == "Hello"
    assert fm["status"] == "done"
    assert body == "Body"


def test_set_frontmatter_overwrite():
    content = "---\nstatus: draft\n---\nBody"
    result = set_frontmatter(content, {"status": "done"})
    fm, _ = extract_frontmatter(result)
    assert fm["status"] == "done"


# ── extract_wikilinks ────────────────────────────────────────────


def test_extract_wikilinks_basic():
    content = "See [[Todo]] and [[Projects/Readme]]"
    links = extract_wikilinks(content)
    assert links == ["Todo", "Projects/Readme"]


def test_extract_wikilinks_alias():
    content = "See [[Todo|my tasks]]"
    links = extract_wikilinks(content)
    assert links == ["Todo"]


def test_extract_wikilinks_heading():
    content = "See [[Todo#section]]"
    links = extract_wikilinks(content)
    assert links == ["Todo"]


def test_extract_wikilinks_dedup():
    content = "[[Todo]] and [[Todo]] again"
    links = extract_wikilinks(content)
    assert links == ["Todo"]


def test_extract_wikilinks_none():
    content = "No links here"
    links = extract_wikilinks(content)
    assert links == []


def test_extract_wikilinks_empty():
    assert extract_wikilinks("") == []


# ── extract_tags ─────────────────────────────────────────────────


def test_extract_tags_inline():
    content = "Some text #project and #urgent"
    tags = extract_tags(content)
    assert "project" in tags
    assert "urgent" in tags


def test_extract_tags_frontmatter_list():
    content = "---\ntags: [project, active]\n---\nBody"
    tags = extract_tags(content)
    assert "project" in tags
    assert "active" in tags


def test_extract_tags_frontmatter_string():
    content = "---\ntags: project, active\n---\nBody"
    tags = extract_tags(content)
    assert "project" in tags
    assert "active" in tags


def test_extract_tags_combined():
    content = "---\ntags: [fm-tag]\n---\nBody #inline-tag"
    tags = extract_tags(content)
    assert tags == ["fm-tag", "inline-tag"]


def test_extract_tags_dedup():
    content = "---\ntags: [project]\n---\n#project"
    tags = extract_tags(content)
    assert tags.count("project") == 1


def test_extract_tags_strips_hash():
    content = "---\ntags: [\"#project\"]\n---\nBody"
    tags = extract_tags(content)
    assert "project" in tags


def test_extract_tags_skips_non_str_int():
    """Dicts/lists in tags field should be skipped, not str()-ified."""
    content = "---\ntags: [good, {bad: dict}, [bad, list]]\n---\nBody"
    tags = extract_tags(content)
    assert tags == ["good"]


def test_extract_tags_int_values():
    content = "---\ntags: [2024, v2]\n---\nBody"
    tags = extract_tags(content)
    assert "2024" in tags
    assert "v2" in tags


def test_extract_tags_nested_path():
    content = "Some text #project/sub-tag"
    tags = extract_tags(content)
    assert "project/sub-tag" in tags


def test_extract_tags_empty():
    assert extract_tags("") == []
    assert extract_tags("no tags here") == []
