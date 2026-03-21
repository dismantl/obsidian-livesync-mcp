---
name: gen-test
description: Generate pytest tests for obsidian-self-mcp following project patterns (async httpx, CouchDB fixtures, respx mocking)
disable-model-invocation: true
---

# Generate Tests

Generate pytest tests for this project. Follow these conventions:

## Test Structure

- Tests go in `tests/` at the repo root
- File naming: `test_<module>.py` (e.g., `test_utils.py`, `test_client.py`)
- Use `pytest` with `pytest-asyncio` (asyncio_mode = "auto" is configured)
- Use `respx` to mock httpx HTTP calls for client tests

## Patterns

### Pure function tests (utils.py, models.py)

```python
import pytest
from obsidian_self_mcp.utils import normalize_doc_id

def test_normalize_doc_id_basic():
    assert normalize_doc_id("Notes/todo.md") == "notes/todo.md"

def test_normalize_doc_id_underscore_prefix():
    """CouchDB reserves _ prefix — LiveSync prepends /."""
    assert normalize_doc_id("_Changelog/entry.md") == "/_changelog/entry.md"
```

### Async client tests (client.py)

Mock CouchDB responses with `respx`:

```python
import pytest
import respx
from httpx import Response
from obsidian_self_mcp.client import ObsidianVaultClient
from obsidian_self_mcp.config import Config

@pytest.fixture
def config():
    return Config(
        couch_url="http://test:5984",
        couch_user="user",
        couch_pass="pass",
        couch_db="test-vault",
    )

@pytest.fixture
def client(config):
    return ObsidianVaultClient(config)

@respx.mock
async def test_read_note(client):
    base = "http://test:5984/test-vault"
    # Mock parent doc
    respx.get(f"{base}/notes%2Ftodo.md").mock(
        return_value=Response(200, json={
            "_id": "notes/todo.md",
            "_rev": "1-abc",
            "children": ["h:abcdefghijkl"],
            "type": "plain",
            "ctime": 1700000000000,
            "mtime": 1700000000000,
            "size": 12,
        })
    )
    # Mock chunk fetch
    respx.post(f"{base}/_all_docs").mock(
        return_value=Response(200, json={
            "rows": [{"id": "h:abcdefghijkl", "doc": {"data": "Hello world!"}}]
        })
    )
    result = await client.read_note("Notes/todo.md")
    assert result.content == "Hello world!"
    assert result.path == "Notes/todo.md"
```

## Key things to mock

- **Parent doc fetch**: `GET /{db}/{encoded_doc_id}` — returns doc with `children` array
- **Chunk fetch**: `POST /{db}/_all_docs?include_docs=true` — returns chunk rows with `data` field
- **Mango search**: `POST /{db}/_find` — returns `docs` array of matching chunks
- **Doc writes**: `PUT /{db}/{encoded_doc_id}` — returns `{"ok": true, "rev": "..."}`
- **Doc deletes**: `DELETE /{db}/{encoded_doc_id}?rev=...`

## What to test

When generating tests for a module, cover:
1. Happy path for each public function/method
2. Edge cases: empty input, underscore-prefixed paths, binary content, missing frontmatter
3. Error handling: 404 responses, 409 conflicts, malformed YAML
4. The chunk reassembly logic (multiple chunks in correct order)
