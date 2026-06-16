# obsidian-livesync-mcp

An MCP server and CLI that gives you direct access to your Obsidian vault through CouchDB — the same database that [Obsidian LiveSync](https://github.com/vrtmrz/obsidian-livesync) uses to sync your notes.

No Obsidian app required. Works on headless servers, in CI pipelines, from AI agents, or anywhere you can run Python.

## Features

- **Full vault CRUD** — read, write, append, delete, and search notes
- **Attachments** — add, get, remove, list, find embeds/orphans, and move/rename binary files with link rewriting
- **Frontmatter & tags** — read/update YAML properties, list and search by tag
- **Backlinks & outbound links** — find connections between notes via content scanning
- **Two transports** — stdio (local) and streamable-http (remote/networked)
- **OAuth 2.1** — OIDC-delegating authorization server for remote MCP clients (claude.ai, etc.)
- **API key auth** — simple Bearer token for headless/CI use
- **CLI included** — `obsidian` command with subcommands for terminal use
- **LiveSync-compatible writes** — proper Rabin-Karp V3 chunking, xxhash64 content-addressed IDs
- **Docker-ready** — single-container deployment

## How it works

If you use Obsidian LiveSync, your vault is already stored in CouchDB. This tool talks directly to that CouchDB instance — reading, writing, searching, and managing notes using the same document/chunk format that LiveSync uses. Changes sync back to Obsidian automatically.

## Who this is for

- **Self-hosted LiveSync users** who want programmatic vault access
- **Homelab operators** running headless servers with no GUI
- **AI agent builders** who need to give Claude, GPT, or other agents access to an Obsidian vault via MCP
- **Automation pipelines** that read/write notes (changelogs, daily notes, project docs)

## Relationship to upstream

This is a fork of [suhasvemuri/obsidian-self-mcp](https://github.com/suhasvemuri/obsidian-self-mcp), which provided a basic proof-of-concept. This project has been substantially rewritten with:

- **LiveSync-compatible writes** — the original only read from CouchDB; writes didn't produce valid LiveSync documents. This fork implements proper Rabin-Karp V3 content-defined chunking and xxhash64 content-addressed chunk IDs, so writes sync back to Obsidian correctly.
- **Correct delete behavior** — the original used CouchDB hard-deletes (`_deleted: true`), which orphaned files on synced devices. Now uses LiveSync soft-delete semantics.
- **Shared chunk safety** — deletes and updates no longer destroy chunks that are still referenced by other notes.
- **Attachment operations** — binary files can be managed through MCP or the CLI without opening Obsidian.
- **Soft-delete awareness** — reads and listings filter out LiveSync-deleted notes instead of surfacing tombstones.
- **OAuth 2.1 support** — full OIDC-delegating authorization server for remote MCP clients.
- **Streamable HTTP transport** — run as a networked server, not just stdio.
- **API key authentication** — Bearer token auth for headless deployments.
- **Comprehensive test suite** — coverage across the client, chunking, OAuth, server, and utilities.
- **Path handling fixes** — correct case preservation and `_`-prefix escaping for doc IDs.
- **Docker support** — containerized deployment.

## Requirements

- Python 3.10+
- A CouchDB instance with Obsidian LiveSync data
- The database name, URL, and credentials

## Installation

```bash
pip install obsidian-livesync-mcp            # core (stdio + HTTP transport)
pip install obsidian-livesync-mcp[oauth]     # with OAuth/OIDC support
```

Or install from source:

```bash
git clone https://github.com/dismantl/obsidian-livesync-mcp.git
cd obsidian-livesync-mcp
pip install -e .            # runtime only
pip install -e ".[oauth]"   # with OAuth/OIDC support (pyjwt + cryptography)
pip install -e ".[dev]"     # with dev tools (ruff, pytest, respx) + OAuth deps
```

## Configuration

Set these environment variables:

```bash
export OBSIDIAN_COUCH_URL="http://your-couchdb-host:5984"
export OBSIDIAN_COUCH_USER="your-username"
export OBSIDIAN_COUCH_PASS="your-password"
export OBSIDIAN_COUCH_DB="obsidian-vault"    # optional, defaults to "obsidian-vault"
```

## MCP Server Setup

The server supports two transports: **stdio** (default, for local clients) and **streamable-http** (for remote/networked access).

### Stdio (Claude Desktop / Claude Code)

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`) or Claude Code settings (`.claude/settings.json`):

```json
{
  "mcpServers": {
    "obsidian-livesync-mcp": {
      "command": "python",
      "args": ["-m", "obsidian_livesync_mcp.server"],
      "env": {
        "OBSIDIAN_COUCH_URL": "http://your-couchdb-host:5984",
        "OBSIDIAN_COUCH_USER": "your-username",
        "OBSIDIAN_COUCH_PASS": "your-password",
        "OBSIDIAN_COUCH_DB": "obsidian-vault"
      }
    }
  }
}
```

### Streamable HTTP

Run as an HTTP server for remote MCP clients:

```bash
export OBSIDIAN_COUCH_URL="http://your-couchdb-host:5984"
export OBSIDIAN_COUCH_USER="your-username"
export OBSIDIAN_COUCH_PASS="your-password"
export MCP_TRANSPORT="streamable-http"
export MCP_HOST="0.0.0.0"    # optional, defaults to 0.0.0.0
export MCP_PORT="8080"        # optional, defaults to 8080
export MCP_API_KEY="your-secret-key"  # optional, enables Bearer token auth
python -m obsidian_livesync_mcp.server
```

When `MCP_API_KEY` is set, clients must include `Authorization: Bearer your-secret-key` in requests. You can also set `MCP_RESOURCE_URL` to the server's public URL (defaults to `http://localhost:{MCP_PORT}`).

### OAuth Authentication (for claude.ai and other remote MCP clients)

For MCP clients that require OAuth (like claude.ai), the server can act as a full OAuth 2.1 authorization server that delegates user authentication to any OIDC provider (Authelia, Keycloak, Auth0, etc.):

```bash
export OBSIDIAN_COUCH_URL="http://your-couchdb-host:5984"
export OBSIDIAN_COUCH_USER="your-username"
export OBSIDIAN_COUCH_PASS="your-password"
export MCP_TRANSPORT="streamable-http"
export MCP_RESOURCE_URL="https://your-mcp-server.example.com"  # public URL

# OIDC provider configuration
export OAUTH_ISSUER_URL="https://your-oidc-provider.example.com"
export OAUTH_CLIENT_ID="your-client-id"         # registered with the OIDC provider
export OAUTH_CLIENT_SECRET="your-client-secret"  # registered with the OIDC provider
export OAUTH_AUTHORIZED_EMAIL="you@example.com"  # required: only this email can access

python -m obsidian_livesync_mcp.server
```

**How it works:**

1. The MCP client (e.g., claude.ai) discovers OAuth endpoints via `/.well-known/oauth-authorization-server`
2. It dynamically registers as a client and initiates the authorization code flow with PKCE
3. You authenticate once via your OIDC provider's login page in a browser
4. The MCP client receives tokens and uses them for all subsequent requests

**Setup requirements:**

- Install with OAuth support: `pip install obsidian-livesync-mcp[oauth]`
- Register the MCP server as a client with your OIDC provider
- Set the redirect URI to `{MCP_RESOURCE_URL}/oauth/callback` (e.g., `https://your-mcp-server.example.com/oauth/callback`)
- The OIDC provider must include the `email` and `email_verified` claims in ID tokens

**OAuth and API key can coexist:** If both `OAUTH_ISSUER_URL` and `MCP_API_KEY` are set, OAuth is the primary auth method and the static API key works as a fallback. This lets claude.ai use OAuth while Claude Code continues using the API key.

| Variable | Required | Description |
|----------|----------|-------------|
| `OAUTH_ISSUER_URL` | For OAuth | OIDC provider's issuer URL (triggers OAuth mode) |
| `OAUTH_CLIENT_ID` | For OAuth | Client ID registered with the OIDC provider |
| `OAUTH_CLIENT_SECRET` | For OAuth | Client secret for the OIDC provider |
| `OAUTH_AUTHORIZED_EMAIL` | For OAuth | Email address authorized to access the vault |
| `MCP_API_KEY` | No | Static Bearer token (works alongside or instead of OAuth) |
| `MCP_RESOURCE_URL` | For OAuth | Public URL of the MCP server |

### Docker

```bash
docker build -t obsidian-livesync-mcp .
docker run -p 8080:8080 \
  -e OBSIDIAN_COUCH_URL="http://your-couchdb-host:5984" \
  -e OBSIDIAN_COUCH_USER="your-username" \
  -e OBSIDIAN_COUCH_PASS="your-password" \
  -e MCP_TRANSPORT="streamable-http" \
  -e MCP_API_KEY="your-secret-key" \
  obsidian-livesync-mcp
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `list_notes` | List notes with metadata, optionally filtered by folder |
| `read_note` | Read the full content of a note |
| `write_note` | Create or update a note |
| `search_notes` | Search note content (case-insensitive) |
| `append_note` | Append content to an existing note |
| `delete_note` | Delete a note with LiveSync-compatible soft-delete semantics |
| `list_folders` | List all folders with note counts |
| `read_frontmatter` | Read frontmatter properties from a note |
| `update_frontmatter` | Set/update frontmatter properties (JSON input) |
| `list_tags` | List all tags in the vault with counts |
| `search_by_tag` | Find notes containing a specific tag |
| `get_backlinks` | Find notes that link to a given note |
| `get_outbound_links` | List wikilinks from a note |
| `add_attachment` | Add or replace a binary attachment from base64 bytes |
| `get_attachment` | Download an attachment as base64, with a 10 MB default size guard |
| `list_attachments` | List binary attachments with metadata |
| `remove_attachment` | Remove an attachment, blocking referenced files unless forced |
| `find_attachment_embeds` | Find notes that embed or link to an attachment |
| `find_orphan_attachments` | List attachments that no note references |
| `move_attachment` | Move or rename an attachment and rewrite note references |

### Attachments

Attachment tools treat binary files as LiveSync `type="newnote"` parent documents. Over MCP, file bytes cross the boundary as base64 strings (`add_attachment`, `get_attachment`). The CLI uses real local files for upload/download. LiveSync plain-text file types (`.md`, `.txt`, `.svg`, `.html`, `.csv`, `.css`, `.js`, `.xml`, `.canvas`) should be managed with the note/text APIs instead of `add_attachment`.

Reference discovery matches by case-insensitive basename, the same limitation as the existing backlink scan. Link rewriting preserves basename-only references, but explicit path references must match the moved attachment path so duplicates in other folders are not rewritten. If two folders contain attachments with the same filename, embed/orphan results are ambiguous.

## CLI Usage

The `obsidian` command provides the same operations from the terminal:

```bash
# List notes
obsidian list
obsidian list "Dev Projects" -n 10
obsidian ls                              # alias

# Read a note
obsidian read "Notes/todo.md"
obsidian cat "Notes/todo.md"             # alias

# Write a note
obsidian write "Notes/new.md" "# Hello"
obsidian write "Notes/new.md" -f local-file.md
echo "content" | obsidian write "Notes/new.md"

# Search
obsidian search "kubernetes" -d "Dev Projects" -n 5
obsidian grep "kubernetes"               # alias

# Append to a note
obsidian append "Notes/log.md" "New entry"

# Delete a note
obsidian delete "Notes/old.md"
obsidian rm "Notes/old.md" -y            # skip confirmation
obsidian prune-orphans                   # dry-run orphan chunk scan
obsidian prune-orphans --delete          # delete only after all devices are synced

# Frontmatter properties
obsidian props "Notes/todo.md"                      # read properties
obsidian props "Notes/todo.md" --set status=done     # set a property
obsidian props "Notes/todo.md" --set 'tags=["a","b"]' status=active

# Tags
obsidian tags                            # list all tags with counts
obsidian tags "Dev Projects"             # tags in a folder
obsidian tags --find "project"           # find notes with a tag

# Backlinks and links
obsidian backlinks "Notes/todo.md"       # notes linking to this note
obsidian links "Notes/todo.md"           # outbound wikilinks from this note

# Attachments
obsidian attach add "Attachments/photo.png" -f ./photo.png
obsidian attach get "Attachments/photo.png" -o /tmp/photo.png
obsidian attach ls Attachments
obsidian attach embeds "Attachments/photo.png"
obsidian attach orphans Attachments
obsidian attach mv "Attachments/photo.png" "Media/photo.png"
obsidian attach rm "Media/photo.png" -y

# Health and repair
obsidian health "Sample/todo.md"
obsidian repair "Sample/todo.md" --from-file ./todo.md
obsidian repair "Sample/photo.png" --from-file ./photo.png --binary

# List folders
obsidian folders
obsidian tree                            # alias
```

## How LiveSync stores data

LiveSync splits each file into a parent document (metadata + ordered list of chunk IDs) and one or more chunk documents (the actual content). Text files use `type="plain"`; binary attachments use `type="newnote"`. This tool handles the parent/chunk model transparently — reads reassemble chunks in order, writes create chunk documents before the parent, and default deletes use LiveSync-compatible soft-delete semantics.

Binary chunks are independently base64-encoded byte ranges. Correct reassembly decodes each chunk and joins bytes; joining base64 strings corrupts multi-chunk binaries.

Document IDs are lowercased vault paths. Paths starting with `_` (like `_Changelog/`) get a `/` prefix since CouchDB reserves `_`-prefixed IDs.

## LiveSync Compatibility

This tool talks directly to CouchDB and must match LiveSync's document format. The following LiveSync settings are **required** for compatibility:

| Setting | Required Value | Default | Notes |
|---------|---------------|---------|-------|
| `encrypt` | `false` | `true` | E2EE not supported — all data would be unreadable |
| `usePathObfuscation` | `false` | `true` | Obfuscated doc IDs not supported |
| `enableCompression` | `false` | `false` | DEFLATE compressed chunks not supported |
| `handleFilenameCaseSensitive` | `false` | `false` | Doc IDs are always lowercased |

> **Important:** LiveSync defaults to E2EE enabled with path obfuscation. Disable both when setting up your vault for use with this tool.

### Compatible Settings

These settings can be any value — reads always work, writes use LiveSync defaults:

| Setting | Our Behavior |
|---------|-------------|
| `hashAlg` | Writes use `xxhash64` (LiveSync default). Reads work with any hash algorithm. |
| `chunkSplitterVersion` | Writes use LiveSync v3 Rabin-Karp chunks. Reads work with any splitter. |
| `customChunkSize` | Reads work with any chunk size. Writes follow LiveSync v3 fixed-unit sizing, so this setting does not change write boundaries. |
| `useEden` | Deprecated. Ignored on read, writes set `eden: {}`. |

### Unsupported Features

| Feature | Impact |
|---------|--------|
| End-to-end encryption (E2EE) | All note content is unreadable |
| Path obfuscation | Cannot locate any documents |
| Data compression (`enableCompression`) | Chunk data appears garbled |
| Chunk packs (`chunkpack` type) | Packed chunks are not fetched |

## License

MIT
