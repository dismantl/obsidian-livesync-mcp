"""Tests for obsidian_livesync_mcp.utils — pure function tests."""

from obsidian_livesync_mcp.utils import (
    encode_doc_id,
    extract_attachment_refs,
    extract_frontmatter,
    extract_tags,
    extract_wikilinks,
    generate_chunk_id,
    normalize_doc_id,
    ref_basename,
    rewrite_attachment_refs,
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


def test_normalize_doc_id_no_obfuscation():
    """Without passphrase, returns lowercased path (LiveSync default)."""
    assert normalize_doc_id("Notes/todo.md") == "notes/todo.md"


def test_normalize_doc_id_no_obfuscation_preserves_structure():
    result = normalize_doc_id("3 Resources/digests/2026-04-03.md")
    assert result == "3 resources/digests/2026-04-03.md"


def test_normalize_doc_id_case_sensitive():
    """With case_insensitive=False, preserves original casing."""
    result = normalize_doc_id("Dev Projects/README.md", case_insensitive=False)
    assert result == "Dev Projects/README.md"


def test_normalize_doc_id_underscore_prefix():
    """CouchDB reserves _ prefix — LiveSync prepends /."""
    assert normalize_doc_id("_Changelog/entry.md") == "/_changelog/entry.md"


def test_normalize_doc_id_strips_leading_slash():
    assert normalize_doc_id("/Notes/todo.md") == "notes/todo.md"


def test_normalize_doc_id_empty():
    assert normalize_doc_id("") == ""


def test_normalize_doc_id_obfuscated():
    """With passphrase, returns f: + SHA-256 hash matching LiveSync."""
    # Known pair: passphrase "undefined", path
    # "Clippings/Streamlining task list processing with Claude MCP.md"
    result = normalize_doc_id(
        "Clippings/Streamlining task list processing with Claude MCP.md",
        obfuscate_passphrase="undefined",
    )
    assert result == "f:014aa7efb7d39ade841b8d94e46304c18fa78962241f91ef132843bae9149ced"


def test_normalize_doc_id_obfuscated_prefix():
    """Obfuscated IDs always start with f: prefix."""
    result = normalize_doc_id("Notes/todo.md", obfuscate_passphrase="secret")
    assert result.startswith("f:")
    assert len(result) == 2 + 64  # f: + 64 hex chars


def test_normalize_doc_id_obfuscated_deterministic():
    """Same path + passphrase always produces the same ID."""
    id1 = normalize_doc_id("test.md", obfuscate_passphrase="pass")
    id2 = normalize_doc_id("test.md", obfuscate_passphrase="pass")
    assert id1 == id2


def test_normalize_doc_id_obfuscated_different_passphrase():
    """Different passphrases produce different IDs for the same path."""
    id1 = normalize_doc_id("test.md", obfuscate_passphrase="pass1")
    id2 = normalize_doc_id("test.md", obfuscate_passphrase="pass2")
    assert id1 != id2


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


# ── attachment references ────────────────────────────────────────


def test_extract_attachment_refs_wikilink_embed():
    content = "Here ![[photo.png]] and ![[dir/diagram.svg|200]] and ![[a.pdf#page=2]]"
    refs = extract_attachment_refs(content)
    assert "photo.png" in refs
    assert "dir/diagram.svg|200" in refs
    assert "a.pdf#page=2" in refs


def test_extract_attachment_refs_markdown_embed():
    content = "![alt](images/pic%20one.jpg) and ![](<my file.png>) and [link](doc.pdf)"
    refs = extract_attachment_refs(content)
    assert "images/pic%20one.jpg" in refs
    assert "<my file.png>" in refs
    assert "doc.pdf" in refs


def test_extract_attachment_refs_ignores_external_markdown_urls():
    content = (
        "![remote](https://example.com/old.png) "
        "![protocol](//example.com/old.png) "
        "[mail](mailto:old.png@example.com)"
    )
    assert extract_attachment_refs(content) == []


def test_extract_attachment_refs_dedup():
    content = "![[x.png]] again ![[x.png]]"
    assert extract_attachment_refs(content).count("x.png") == 1


def test_ref_basename_normalizes():
    assert ref_basename("dir/diagram.svg|200") == "diagram.svg"
    assert ref_basename("a.pdf#page=2") == "a.pdf"
    assert ref_basename("images/pic%20one.jpg") == "pic one.jpg"
    assert ref_basename("<my file.png>") == "my file.png"
    assert ref_basename('path/to/x.png "a title"') == "x.png"
    assert ref_basename("PHOTO.PNG") == "photo.png"


def test_rewrite_attachment_refs_wikilink_basename():
    content = "before ![[old.png|120]] after"
    new, count = rewrite_attachment_refs(content, "att/old.png", "att/new.png")
    assert count == 1
    assert "![[new.png|120]]" in new


def test_rewrite_attachment_refs_wikilink_full_path():
    content = "![[att/old.png]]"
    new, count = rewrite_attachment_refs(content, "att/old.png", "media/new.png")
    assert count == 1
    assert "![[media/new.png]]" in new


def test_rewrite_attachment_refs_markdown():
    content = "![cap](att/old.png) and ![](old.png)"
    new, count = rewrite_attachment_refs(content, "att/old.png", "media/new.png")
    assert count == 2
    assert "![cap](media/new.png)" in new
    assert "![](new.png)" in new


def test_rewrite_attachment_refs_markdown_preserves_fragment():
    content = "[pdf](att/old.pdf#page=2) and ![](old.png#crop=10)"
    new, count = rewrite_attachment_refs(content, "att/old.pdf", "media/new.pdf")
    assert count == 1
    assert "[pdf](media/new.pdf#page=2)" in new
    assert "![](old.png#crop=10)" in new


def test_rewrite_attachment_refs_leaves_external_markdown_urls():
    content = "![remote](https://example.com/old.png) and ![protocol](//example.com/old.png)"
    new, count = rewrite_attachment_refs(content, "att/old.png", "media/new.png")
    assert count == 0
    assert new == content


def test_rewrite_attachment_refs_no_match_unchanged():
    content = "![[unrelated.png]]"
    new, count = rewrite_attachment_refs(content, "att/old.png", "att/new.png")
    assert count == 0
    assert new == content


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
    content = '---\ntags: ["#project"]\n---\nBody'
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
