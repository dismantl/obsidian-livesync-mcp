"""FastMCP server exposing Obsidian vault tools via stdio or streamable-http transport."""

import asyncio
import functools
import logging
import os

import httpx
from mcp.server.fastmcp import FastMCP

from .client import ObsidianVaultClient
from .config import Config

logger = logging.getLogger(__name__)

_transport = os.environ.get("MCP_TRANSPORT", "stdio")
_server_kwargs: dict = {}
_oauth_provider = None
_oauth_store = None

if _transport == "streamable-http":
    _server_kwargs["host"] = os.environ.get("MCP_HOST", "0.0.0.0")
    _server_kwargs["port"] = int(os.environ.get("MCP_PORT", "8080"))
    _server_kwargs["stateless_http"] = True
    _server_kwargs["json_response"] = True

    _api_key = os.environ.get("MCP_API_KEY", "")
    _port = int(os.environ.get("MCP_PORT", "8080"))
    _resource_url = os.environ.get("MCP_RESOURCE_URL", f"http://localhost:{_port}")

    if os.environ.get("OAUTH_ISSUER_URL"):
        _config = Config()
        # OAuth mode: full OAuthAuthorizationServerProvider with OIDC delegation
        from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
        from pydantic import AnyHttpUrl

        from .oauth_provider import OIDCDelegatingProvider
        from .oauth_store import OAuthStore

        _oauth_store = OAuthStore(
            couch_url=_config.couch_url,
            couch_user=_config.couch_user,
            couch_pass=_config.couch_pass,
        )
        _http_client = httpx.AsyncClient(timeout=30.0)
        _oauth_provider = OIDCDelegatingProvider(
            config=_config,
            store=_oauth_store,
            http_client=_http_client,
            resource_url=_resource_url,
            api_key=_api_key or None,
        )

        _server_kwargs["auth_server_provider"] = _oauth_provider
        _server_kwargs["auth"] = AuthSettings(
            issuer_url=AnyHttpUrl(_resource_url),
            resource_server_url=AnyHttpUrl(_resource_url),
            required_scopes=[],
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

        logger.info("OAuth mode enabled (OIDC issuer: %s)", _config.oauth_issuer_url)

    elif _api_key:
        # Static API key mode (existing behavior)
        from mcp.server.auth.provider import AccessToken, TokenVerifier
        from mcp.server.auth.settings import AuthSettings
        from pydantic import AnyHttpUrl

        class _APIKeyVerifier(TokenVerifier):
            """Verify Bearer tokens against MCP_API_KEY env var."""

            async def verify_token(self, token: str) -> AccessToken | None:
                if token != _api_key:
                    return None
                return AccessToken(token=token, client_id="api-key", scopes=[], expires_at=None)

        _server_kwargs["token_verifier"] = _APIKeyVerifier()
        _server_kwargs["auth"] = AuthSettings(
            issuer_url=AnyHttpUrl(_resource_url),
            resource_server_url=AnyHttpUrl(_resource_url),
            required_scopes=[],
        )

mcp = FastMCP("obsidian-self-mcp", **_server_kwargs)

# Mount OAuth callback route when in OAuth mode
if _oauth_provider is not None:
    from starlette.requests import Request
    from starlette.responses import Response

    from .oauth_callback import handle_oauth_callback

    _provider_ref = _oauth_provider

    @mcp.custom_route("/oauth/callback", methods=["GET"])
    async def oauth_callback(request: Request) -> Response:
        return await handle_oauth_callback(request, _provider_ref)


_client: ObsidianVaultClient | None = None


def _get_client() -> ObsidianVaultClient:
    global _client
    if _client is None:
        _client = ObsidianVaultClient(Config())
    return _client


def _tool_error_handler(func):
    """Wrap MCP tool functions to return friendly error strings."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except ValueError as e:
            return f"Error: {e}"
        except httpx.HTTPStatusError as e:
            logger.exception("CouchDB request failed")
            return f"Error: CouchDB returned {e.response.status_code}"
        except httpx.ConnectError:
            return "Error: Could not connect to CouchDB. Check OBSIDIAN_COUCH_URL."
        except Exception as e:
            logger.exception("Unexpected error in tool %s", func.__name__)
            return f"Error: {type(e).__name__}: {e}"

    return wrapper


@mcp.tool()
@_tool_error_handler
async def list_notes(folder: str | None = None, limit: int = 50, skip: int = 0) -> str:
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
@_tool_error_handler
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
@_tool_error_handler
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
@_tool_error_handler
async def search_notes(query: str, folder: str | None = None, limit: int = 20) -> str:
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
@_tool_error_handler
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
@_tool_error_handler
async def delete_note(path: str, hard: bool = False) -> str:
    """Delete a note from the Obsidian vault.

    Defaults to a livesync-compatible soft-delete: sets `deleted: True` on
    the document and preserves its chunks. This is the form the
    obsidian-livesync plugin emits for its own deletes, and it is the only
    form that propagates to filesystem copies on livesync-connected devices.

    Pass `hard=True` ONLY for broken-manifest cleanup (e.g. recovering from
    "missing N chunks" errors). Hard-delete creates a CouchDB tombstone and
    removes chunks, but does NOT propagate to filesystem copies on
    livesync-connected devices — orphaned files will remain on disk until
    manually removed.

    Args:
        path: Vault path to the note to delete
        hard: If True, use CouchDB hard-delete with chunk cleanup. Default
            False (soft-delete, the livesync-compatible path).
    """
    client = _get_client()
    await client.delete_note(path, hard=hard)
    return f"Deleted: {path}" + (" (hard)" if hard else "")


@mcp.tool()
@_tool_error_handler
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
@_tool_error_handler
async def update_frontmatter(path: str, properties_json: str) -> str:
    """Update or set frontmatter properties on a note.

    Args:
        path: Vault path to the note
        properties_json: JSON string of properties to set
            (e.g. '{"status": "done", "tags": ["project", "active"]}')
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
@_tool_error_handler
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
@_tool_error_handler
async def search_by_tag(tag: str, folder: str | None = None, limit: int = 20) -> str:
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
@_tool_error_handler
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
@_tool_error_handler
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
@_tool_error_handler
async def list_folders() -> str:
    """List all folders in the Obsidian vault with note counts."""
    client = _get_client()
    folders = await client.list_folders()
    if not folders:
        return "No folders found."
    lines = [f"{f.path}/  ({f.note_count} notes)" for f in folders]
    return f"Found {len(folders)} folders:\n" + "\n".join(lines)


async def _initialize_oauth() -> None:
    """Initialize OAuth store and provider. Called once at server startup."""
    if _oauth_store is not None and _oauth_provider is not None:
        await _oauth_store.ensure_db()
        await _oauth_provider.initialize()
        _oauth_store.start_purge_task()


def main():
    # Perform deferred async initialization for OAuth
    if _oauth_store is not None:
        asyncio.run(_initialize_oauth())

    try:
        if _transport == "streamable-http":
            mcp.run(transport="streamable-http")
        else:
            mcp.run(transport="stdio")
    finally:
        if _oauth_store is not None:
            _oauth_store.stop_purge_task()


if __name__ == "__main__":
    main()
