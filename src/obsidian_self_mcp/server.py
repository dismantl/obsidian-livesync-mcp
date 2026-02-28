"""FastMCP server exposing Obsidian vault tools via stdio transport."""

from mcp.server.fastmcp import FastMCP

from .client import ObsidianVaultClient
from .config import Config

mcp = FastMCP("obsidian-self-mcp")
_client: ObsidianVaultClient | None = None


def _get_client() -> ObsidianVaultClient:
    global _client
    if _client is None:
        _client = ObsidianVaultClient(Config())
    return _client


@mcp.tool()
async def list_notes(
    folder: str | None = None, limit: int = 50, skip: int = 0
) -> str:
    """List notes in the Obsidian vault with metadata.

    Args:
        folder: Optional folder path to filter (e.g. "Dev Projects/Arrmada")
        limit: Max notes to return (default 50)
        skip: Number of notes to skip for pagination
    """
    client = _get_client()
    notes = await client.list_notes(folder=folder, limit=limit, skip=skip)
    if not notes:
        return "No notes found."
    lines = [f"{n.path}  ({n.size} bytes, {n.chunk_count} chunks)" for n in notes]
    return f"Found {len(notes)} notes:\n" + "\n".join(lines)


@mcp.tool()
async def read_note(path: str) -> str:
    """Read the full content of a note from the Obsidian vault.

    Args:
        path: Vault path to the note (e.g. "Dev Projects/Arrmada/README.md")
    """
    client = _get_client()
    note = await client.read_note(path)
    if not note:
        return f"Note not found: {path}"
    if note.is_binary:
        return f"Binary file ({note.size} bytes). Content is base64 encoded."
    return note.content


@mcp.tool()
async def write_note(path: str, content: str) -> str:
    """Create or update a note in the Obsidian vault.

    Args:
        path: Vault path for the note (e.g. "Notes/test.md")
        content: Text content to write
    """
    client = _get_client()
    await client.write_note(path, content)
    return f"Written: {path} ({len(content.encode('utf-8'))} bytes)"


@mcp.tool()
async def search_notes(
    query: str, folder: str | None = None, limit: int = 20
) -> str:
    """Search note content in the Obsidian vault.

    Args:
        query: Text to search for (case-insensitive)
        folder: Optional folder to restrict search
        limit: Max results to return (default 20)
    """
    client = _get_client()
    results = await client.search_notes(query=query, folder=folder, limit=limit)
    if not results:
        return f"No results for: {query}"
    lines = []
    for r in results:
        lines.append(f"\n## {r.path} ({r.matches} matches)")
        for s in r.snippets:
            lines.append(f"  > {s}")
    return f"Found matches in {len(results)} notes:" + "\n".join(lines)


@mcp.tool()
async def append_note(path: str, content: str) -> str:
    """Append content to an existing note in the Obsidian vault.

    Args:
        path: Vault path to the note
        content: Text to append
    """
    client = _get_client()
    await client.append_note(path, content)
    return f"Appended to: {path}"


@mcp.tool()
async def delete_note(path: str) -> str:
    """Delete a note and its chunks from the Obsidian vault.

    Args:
        path: Vault path to the note to delete
    """
    client = _get_client()
    await client.delete_note(path)
    return f"Deleted: {path}"


@mcp.tool()
async def read_frontmatter(path: str) -> str:
    """Read frontmatter properties from a note.

    Args:
        path: Vault path to the note (e.g. "Notes/todo.md")
    """
    client = _get_client()
    fm = await client.read_frontmatter(path)
    if fm is None:
        return f"No frontmatter found in: {path}"
    lines = [f"{k}: {v}" for k, v in fm.items()]
    return f"Frontmatter for {path}:\n" + "\n".join(lines)


@mcp.tool()
async def update_frontmatter(path: str, properties_json: str) -> str:
    """Update or set frontmatter properties on a note.

    Args:
        path: Vault path to the note
        properties_json: JSON string of properties to set (e.g. '{"status": "done", "tags": ["project", "active"]}')
    """
    import json
    try:
        properties = json.loads(properties_json)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    if not isinstance(properties, dict):
        return "properties_json must be a JSON object"
    client = _get_client()
    await client.update_frontmatter(path, properties)
    return f"Updated frontmatter for: {path}"


@mcp.tool()
async def list_tags(folder: str | None = None) -> str:
    """List all tags in the vault with occurrence counts.

    Args:
        folder: Optional folder to restrict scan
    """
    client = _get_client()
    tags = await client.list_tags(folder=folder)
    if not tags:
        return "No tags found."
    lines = [f"  #{tag}  ({count})" for tag, count in tags.items()]
    return f"Found {len(tags)} tags:\n" + "\n".join(lines)


@mcp.tool()
async def search_by_tag(
    tag: str, folder: str | None = None, limit: int = 20
) -> str:
    """Find notes containing a specific tag.

    Args:
        tag: Tag to search for (with or without #)
        folder: Optional folder to restrict search
        limit: Max results (default 20)
    """
    client = _get_client()
    notes = await client.search_by_tag(tag=tag, folder=folder, limit=limit)
    if not notes:
        return f"No notes found with tag: #{tag}"
    lines = [f"  {n.path}" for n in notes]
    return f"Found {len(notes)} notes with #{tag}:\n" + "\n".join(lines)


@mcp.tool()
async def get_backlinks(path: str) -> str:
    """Find notes that link to this note via wikilinks.

    Args:
        path: Vault path to the target note
    """
    client = _get_client()
    backlinks = await client.get_backlinks(path)
    if not backlinks:
        return f"No backlinks found for: {path}"
    lines = []
    for bl in backlinks:
        ctx = f" — {bl.context}" if bl.context else ""
        lines.append(f"  {bl.source_path}{ctx}")
    return f"Found {len(backlinks)} backlinks for {path}:\n" + "\n".join(lines)


@mcp.tool()
async def get_outbound_links(path: str) -> str:
    """List wikilinks from a note (outbound links).

    Args:
        path: Vault path to the note
    """
    client = _get_client()
    links = await client.get_outbound_links(path)
    if not links:
        return f"No outbound links in: {path}"
    lines = [f"  [[{link}]]" for link in links]
    return f"Found {len(links)} outbound links in {path}:\n" + "\n".join(lines)


@mcp.tool()
async def list_folders() -> str:
    """List all folders in the Obsidian vault with note counts."""
    client = _get_client()
    folders = await client.list_folders()
    if not folders:
        return "No folders found."
    lines = [f"{f.path}/  ({f.note_count} notes)" for f in folders]
    return f"Found {len(folders)} folders:\n" + "\n".join(lines)


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
