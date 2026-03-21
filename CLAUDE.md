# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MCP server and CLI for accessing Obsidian vaults through CouchDB (used by Obsidian LiveSync). No Obsidian app required. Python 3.10+.

## Commands

```bash
# Install for development (with dev tools: ruff, pytest, respx)
pip install -e ".[dev]"

# Run MCP server (stdio transport)
python -m obsidian_self_mcp.server

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

## Required Environment Variables

```bash
OBSIDIAN_COUCH_URL=http://localhost:5984
OBSIDIAN_COUCH_USER=username
OBSIDIAN_COUCH_PASS=password
OBSIDIAN_COUCH_DB=obsidian-vault  # optional, defaults to "obsidian-vault"
```

Fallback names also supported: `COUCHDB_URL`, `COUCHDB_USER`, `COUCHDB_PASSWORD`, `COUCHDB_DB`.

## Architecture

All source code lives under `src/obsidian_self_mcp/`. There are two entry points that share a common async client:

- **`server.py`** — FastMCP server exposing 13 tools over stdio. Uses a lazy-initialized global `ObsidianClient` singleton.
- **`cli.py`** — Argparse CLI (`obsidian` command) with subcommands. Runs async operations via `asyncio.run()`.

Both delegate all CouchDB interaction to:

- **`client.py`** — `ObsidianVaultClient` class. Async HTTP client (`httpx.AsyncClient`) that handles all CRUD, search, frontmatter, tags, and backlink operations. This is where the core business logic lives.

Supporting modules:

- **`config.py`** — Frozen dataclass reading env vars at startup.
- **`models.py`** — Data classes (`NoteMetadata`, `NoteContent`, `SearchResult`, `BacklinkInfo`, `FolderInfo`).
- **`utils.py`** — Path normalization, chunk ID generation, frontmatter/YAML parsing, wikilink and tag extraction.

## LiveSync Document Model

Understanding this is essential for working on `client.py` or `utils.py`:

- Each note is stored as a **parent document** (CouchDB doc with `_id` = lowercased vault path) containing a `children` array of chunk IDs.
- **Chunk documents** hold the actual content (`_id` = `"h:" + 12-char alphanumeric`, `type` = `"leaf"`).
- Text notes are typically one chunk. Binary files are base64-encoded and split into ~10KB chunks.
- Paths starting with `_` (e.g., `_Changelog/`) get a `/` prefix because CouchDB reserves `_`-prefixed IDs.
- Reads must reassemble chunks in order. Writes must create chunk docs before the parent. Deletes must clean up both.

## Key Patterns

- **Conflict handling** — writes retry on HTTP 409 (CouchDB revision conflicts).
- **Search** — uses CouchDB Mango queries with regex on chunk data, then maps matching chunks back to parent notes via a reverse chunk-to-parent map.
- **Frontmatter** — parsed via regex extraction of `---\nYAML\n---` blocks, then `yaml.safe_load`.
- **Wikilinks/tags** — extracted via regex from note content (and frontmatter `tags:` field for tags).
