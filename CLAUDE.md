# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP server and CLI for accessing Obsidian vaults through CouchDB (used by Obsidian LiveSync). No Obsidian app required. Python 3.10+.

## Commands

```bash
# Install for development (with dev tools: ruff, pytest, respx)
pip install -e ".[dev]"

# Run MCP server (stdio transport, default)
python -m obsidian_self_mcp.server

# Run MCP server (HTTP transport)
MCP_TRANSPORT=streamable-http python -m obsidian_self_mcp.server

# Run CLI
obsidian <command> [args]

# Lint and format
ruff check .                  # lint
ruff check --fix .            # lint with auto-fix
ruff format .                 # format

# Tests
pytest                        # run all tests
pytest tests/test_utils.py    # run a single test file
pytest -k test_normalize      # run tests matching a name
```

## Testing Patterns

- **ASGI functional tests** — `test_server.py::TestStreamableHttpASGI` uses Starlette `TestClient` to hit the real MCP app in-process (no subprocess/port). Uses `host='0.0.0.0'` to avoid DNS rebinding protection.
- **Module-level config** — server config is evaluated at import time. Tests that change transport mode must `importlib.reload()` the server module with patched env vars (see `_reload_server_module` helper).
- **OAuth tests** — use `respx` to mock OIDC/CouchDB HTTP calls and `AsyncMock(spec=OAuthStore)` for the store. ID token tests generate real RSA keypairs via `cryptography` and sign JWTs with `pyjwt`. All ID token test claims must include `email_verified: True`.

## Required Environment Variables

```bash
OBSIDIAN_COUCH_URL=http://localhost:5984
OBSIDIAN_COUCH_USER=username
OBSIDIAN_COUCH_PASS=password
OBSIDIAN_COUCH_DB=obsidian-vault  # optional, defaults to "obsidian-vault"
```

Fallback names also supported: `COUCHDB_URL`, `COUCHDB_USER`, `COUCHDB_PASSWORD`, `COUCHDB_DB`.

### OAuth Environment Variables (all required when `OAUTH_ISSUER_URL` is set)

```bash
OAUTH_ISSUER_URL=https://your-oidc-provider.example.com
OAUTH_CLIENT_ID=your-client-id
OAUTH_CLIENT_SECRET=your-client-secret
OAUTH_AUTHORIZED_EMAIL=you@example.com
```

## Architecture

All source code lives under `src/obsidian_self_mcp/`. There are two entry points that share a common async client:

- **`server.py`** — FastMCP server exposing 13 tools over stdio. Uses a lazy-initialized global `ObsidianVaultClient` singleton.
- **`cli.py`** — Argparse CLI (`obsidian` command) with subcommands. Runs async operations via `asyncio.run()`.

Both delegate all CouchDB interaction to:

- **`client.py`** — `ObsidianVaultClient` class. Async HTTP client (`httpx.AsyncClient`) that handles all CRUD, search, frontmatter, tags, and backlink operations. This is where the core business logic lives.

Supporting modules:

- **`config.py`** — Frozen dataclass reading env vars at startup.
- **`models.py`** — Data classes (`NoteMetadata`, `NoteContent`, `SearchResult`, `BacklinkInfo`, `FolderInfo`).
- **`utils.py`** — Path normalization, chunk ID generation, frontmatter/YAML parsing, wikilink and tag extraction.

### OAuth Subsystem (optional)

Enabled when `OAUTH_ISSUER_URL` is set. Adds OAuth 2.1 authorization server delegating to an upstream OIDC provider:

- **`oauth_provider.py`** — `OIDCDelegatingProvider` implements MCP SDK's `OAuthAuthorizationServerProvider`. Takes `resource_url` as constructor param. Uses `EphemeralStore` for short-lived auth codes/state.
- **`oauth_store.py`** — `OAuthStore` persists clients, access tokens, and refresh tokens in a separate `mcp_oauth` CouchDB database. Owns cascading revocation via `delete_paired_tokens()`.
- **`oauth_callback.py`** — Starlette route handler for OIDC redirect. Validates ID token (signature, issuer, audience, `email_verified`), checks authorized email, issues MCP auth code.
- **`config.py`** — When `oauth_issuer_url` is set, `oauth_client_id`, `oauth_client_secret`, and `oauth_authorized_email` are all required.
- OAuth initialization is **deferred** — `asyncio.run(_initialize_oauth())` happens in `main()`, not at import time. This preserves `importlib.reload()` compatibility in tests.
- `pyjwt[crypto]` is an optional dependency (`[oauth]` extra). Dev deps include it via `[dev]`.

## LiveSync Document Model

Understanding this is essential for working on `client.py`, `utils.py`, or `chunking.py`:

- Each note is stored as a **parent document** (CouchDB doc with `_id` = lowercased vault path) containing a `children` array of chunk IDs.
- **Chunk documents** hold the actual content (`_id` = `"h:" + xxhash64_base36`, `type` = `"leaf"`). Chunk IDs are content-hash based — same content always produces the same ID.
- Content is split using **Rabin-Karp V3** content-defined chunking (PRIME=31, window=48 bytes, boundary when `hash % avgChunkSize == 1`). Text avg chunk = max(128B, size/20). Binary avg chunk = max(4KB, size/12).
- Legacy documents (type `"notes"`) store content directly in a `data` field instead of chunks.
- Paths starting with `_` (e.g., `_Changelog/`) get a `/` prefix because CouchDB reserves `_`-prefixed IDs.
- Reads must reassemble chunks in order. Writes must create chunk docs before the parent. Updates clean up orphaned chunks. Deletes must clean up both.

### Required LiveSync Settings

These settings **must** be configured for compatibility:
- `encrypt: false` — no E2EE support
- `usePathObfuscation: false` — no path deobfuscation
- `enableCompression: false` — no DEFLATE decompression
- `handleFilenameCaseSensitive: false` — doc IDs are always lowercased

## Key Patterns

- **Server config** — transport mode (`MCP_TRANSPORT`), host/port, and auth are configured at module level via `FastMCP(...)` constructor kwargs in `_server_kwargs`. `mcp.run()` only takes `transport`.
- **Conflict handling** — writes retry on HTTP 409 (CouchDB revision conflicts).
- **Search** — uses CouchDB Mango queries with regex on chunk data, then maps matching chunks back to parent notes via a reverse chunk-to-parent map.
- **Frontmatter** — parsed via regex extraction of `---\nYAML\n---` blocks, then `yaml.safe_load`.
- **Wikilinks/tags** — extracted via regex from note content (and frontmatter `tags:` field for tags).
