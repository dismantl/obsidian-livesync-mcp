# obsidian-self-mcp

An MCP server and CLI that gives you direct access to your Obsidian vault through CouchDB — the same database that [Obsidian LiveSync](https://github.com/vrtmrz/obsidian-livesync) uses to sync your notes.

No Obsidian app required. Works on headless servers, in CI pipelines, from AI agents, or anywhere you can run Python.

## How it works

If you use Obsidian LiveSync, your vault is already stored in CouchDB. This tool talks directly to that CouchDB instance — reading, writing, searching, and managing notes using the same document/chunk format that LiveSync uses. Changes sync back to Obsidian automatically.

## Who this is for

- **Self-hosted LiveSync users** who want programmatic vault access
- **Homelab operators** running headless servers with no GUI
- **AI agent builders** who need to give Claude, GPT, or other agents access to an Obsidian vault via MCP
- **Automation pipelines** that read/write notes (changelogs, daily notes, project docs)

## How this differs from Obsidian's official CLI

Obsidian has an [official CLI](https://obsidian.md/blog/introducing-obsidian-cli/) that requires the Obsidian desktop app running locally and a Catalyst license. This project requires neither — just a CouchDB instance with LiveSync data.

| Feature | Official CLI | obsidian-self-mcp |
|---------|-------------|-------------------|
| **Requires Obsidian app** | Yes (must be running) | No |
| **Requires Catalyst license** | Yes ($25+) | No (MIT, free) |
| **Read/write notes** | Yes | Yes |
| **Search** | Yes | Yes |
| **Frontmatter/properties** | Yes | Yes |
| **Tags** | Yes | Yes |
| **Backlinks** | Yes (via app index) | Yes (content scanning) |
| **Templates** | Yes | No (planned) |
| **Canvas** | Yes | No |
| **Graph view** | No | No |
| **Works headless/CI** | No | Yes |
| **MCP server** | No | Yes |
| **Transport** | Local REST API | CouchDB (network) |

## Requirements

- Python 3.10+
- A CouchDB instance with Obsidian LiveSync data
- The database name, URL, and credentials

## Installation

```bash
pip install obsidian-self-mcp
```

Or install from source:

```bash
git clone https://github.com/suhasvemuri/obsidian-self-mcp.git
cd obsidian-self-mcp
pip install -e .          # runtime only
pip install -e ".[dev]"   # with ruff, pytest, respx
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
    "obsidian-self-mcp": {
      "command": "python",
      "args": ["-m", "obsidian_self_mcp.server"],
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
python -m obsidian_self_mcp.server
```

When `MCP_API_KEY` is set, clients must include `Authorization: Bearer your-secret-key` in requests. You can also set `MCP_RESOURCE_URL` to override the OAuth resource server URL (defaults to `http://localhost:{MCP_PORT}`).

### Docker

```bash
docker build -t obsidian-self-mcp .
docker run -p 8080:8080 \
  -e OBSIDIAN_COUCH_URL="http://your-couchdb-host:5984" \
  -e OBSIDIAN_COUCH_USER="your-username" \
  -e OBSIDIAN_COUCH_PASS="your-password" \
  -e MCP_TRANSPORT="streamable-http" \
  -e MCP_API_KEY="your-secret-key" \
  obsidian-self-mcp
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `list_notes` | List notes with metadata, optionally filtered by folder |
| `read_note` | Read the full content of a note |
| `write_note` | Create or update a note |
| `search_notes` | Search note content (case-insensitive) |
| `append_note` | Append content to an existing note |
| `delete_note` | Delete a note and its chunks |
| `list_folders` | List all folders with note counts |
| `read_frontmatter` | Read frontmatter properties from a note |
| `update_frontmatter` | Set/update frontmatter properties (JSON input) |
| `list_tags` | List all tags in the vault with counts |
| `search_by_tag` | Find notes containing a specific tag |
| `get_backlinks` | Find notes that link to a given note |
| `get_outbound_links` | List wikilinks from a note |

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

# List folders
obsidian folders
obsidian tree                            # alias
```

## How LiveSync stores data

LiveSync splits each note into a parent document (metadata + ordered list of chunk IDs) and one or more chunk documents (the actual content). This tool handles all of that transparently — reads reassemble chunks in order, writes create proper chunk documents, and deletes clean up both the parent and all chunks.

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
| `chunkSplitterVersion` | Writes use `v3-rabin-karp` (LiveSync default). Reads work with any splitter. |
| `customChunkSize` | Writes use `0` (default). Reads work with any chunk size. |
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
