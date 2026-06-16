"""Tests for obsidian_livesync_mcp.server — error handler, API key verifier, and ASGI startup."""

import importlib
from unittest.mock import patch

import httpx
import pytest
from httpx import Request, Response
from starlette.testclient import TestClient

from obsidian_livesync_mcp.server import _tool_error_handler

# ── _tool_error_handler ──────────────────────────────────────────


async def test_error_handler_value_error():
    @_tool_error_handler
    async def failing():
        raise ValueError("Note not found: test.md")

    result = await failing()
    assert result == "Error: Note not found: test.md"


async def test_error_handler_http_status_error():
    @_tool_error_handler
    async def failing():
        resp = Response(500, request=Request("GET", "http://test/db"))
        raise httpx.HTTPStatusError("Server Error", request=resp.request, response=resp)

    result = await failing()
    assert result == "Error: CouchDB returned 500"


async def test_error_handler_connect_error():
    @_tool_error_handler
    async def failing():
        raise httpx.ConnectError("Connection refused")

    result = await failing()
    assert result == "Error: Could not connect to CouchDB. Check OBSIDIAN_COUCH_URL."


async def test_error_handler_generic_exception():
    @_tool_error_handler
    async def failing():
        raise RuntimeError("something broke")

    result = await failing()
    assert result == "Error: RuntimeError: something broke"


async def test_error_handler_passes_through_on_success():
    @_tool_error_handler
    async def succeeding():
        return "all good"

    result = await succeeding()
    assert result == "all good"


# ── _APIKeyVerifier ──────────────────────────────────────────────


@pytest.fixture
def api_key_verifier():
    """Create an _APIKeyVerifier instance for testing."""
    from mcp.server.auth.provider import AccessToken, TokenVerifier

    class TestVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> AccessToken | None:
            if token != "test-secret":
                return None
            return AccessToken(token=token, client_id="api-key", scopes=[], expires_at=None)

    return TestVerifier()


async def test_api_key_verifier_valid_token(api_key_verifier):
    result = await api_key_verifier.verify_token("test-secret")
    assert result is not None
    assert result.token == "test-secret"
    assert result.client_id == "api-key"


async def test_api_key_verifier_invalid_token(api_key_verifier):
    result = await api_key_verifier.verify_token("wrong-key")
    assert result is None


async def test_api_key_verifier_empty_token(api_key_verifier):
    result = await api_key_verifier.verify_token("")
    assert result is None


# ── Regression: host/port passed to FastMCP constructor, not run() ──


def _reload_server_module(env_overrides: dict) -> object:
    """Reload the server module with custom env vars to pick up module-level config."""
    import obsidian_livesync_mcp.server as mod

    with patch.dict("os.environ", env_overrides, clear=False):
        importlib.reload(mod)
    return mod


class TestStreamableHttpConfig:
    """Ensure host/port are set on FastMCP settings, not passed to run()."""

    def test_host_port_on_settings_defaults(self):
        mod = _reload_server_module({"MCP_TRANSPORT": "streamable-http"})
        try:
            assert mod.mcp.settings.host == "0.0.0.0"
            assert mod.mcp.settings.port == 8080
        finally:
            _reload_server_module({"MCP_TRANSPORT": "stdio"})

    def test_host_port_on_settings_custom(self):
        mod = _reload_server_module(
            {
                "MCP_TRANSPORT": "streamable-http",
                "MCP_HOST": "127.0.0.1",
                "MCP_PORT": "9090",
            }
        )
        try:
            assert mod.mcp.settings.host == "127.0.0.1"
            assert mod.mcp.settings.port == 9090
        finally:
            _reload_server_module({"MCP_TRANSPORT": "stdio"})

    def test_main_calls_run_without_host_port(self):
        """run() must only receive 'transport', never host/port (the original bug)."""
        mod = _reload_server_module({"MCP_TRANSPORT": "streamable-http"})
        try:
            with patch.object(mod.mcp, "run") as mock_run:
                mod.main()
                mock_run.assert_called_once_with(transport="streamable-http")
        finally:
            _reload_server_module({"MCP_TRANSPORT": "stdio"})

    def test_stdio_main_calls_run_without_host_port(self):
        mod = _reload_server_module({"MCP_TRANSPORT": "stdio"})
        with patch.object(mod.mcp, "run") as mock_run:
            mod.main()
            mock_run.assert_called_once_with(transport="stdio")


# ── Functional: ASGI app starts and handles MCP protocol ────────

_MCP_HEADERS = {"Accept": "application/json"}

_INIT_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.1"},
    },
}

_TOOLS_LIST_REQUEST = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {},
}


class TestStreamableHttpASGI:
    """Functional tests: build the real Starlette app and hit it via TestClient.

    These catch config/wiring bugs (like passing bad kwargs to FastMCP)
    that unit-level mocks would miss.
    """

    @pytest.fixture()
    def http_server_module(self):
        """Reload server module in streamable-http mode and yield it."""
        mod = _reload_server_module({"MCP_TRANSPORT": "streamable-http"})
        yield mod
        _reload_server_module({"MCP_TRANSPORT": "stdio"})

    def test_app_starts_and_accepts_initialize(self, http_server_module):
        """The ASGI app must start without errors and respond to MCP initialize."""
        app = http_server_module.mcp.streamable_http_app()
        with TestClient(app) as client:
            resp = client.post("/mcp", json=_INIT_REQUEST, headers=_MCP_HEADERS)
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"]["serverInfo"]["name"] == "obsidian-livesync-mcp"

    def test_app_lists_registered_tools(self, http_server_module):
        """All MCP tools should be visible through the ASGI app."""
        app = http_server_module.mcp.streamable_http_app()
        with TestClient(app) as client:
            # Initialize first (required by protocol)
            client.post("/mcp", json=_INIT_REQUEST, headers=_MCP_HEADERS)
            resp = client.post("/mcp", json=_TOOLS_LIST_REQUEST, headers=_MCP_HEADERS)
        assert resp.status_code == 200
        tools = resp.json()["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        expected = {
            "list_notes",
            "get_file_info",
            "read_note",
            "read_note_range",
            "write_note",
            "search_notes",
            "append_note",
            "delete_note",
            "read_frontmatter",
            "update_frontmatter",
            "list_tags",
            "search_by_tag",
            "get_backlinks",
            "get_outbound_links",
            "list_folders",
            "add_attachment",
            "get_attachment",
            "get_attachment_range",
            "list_attachments",
            "create_download_url",
            "create_upload_url",
            "remove_attachment",
            "find_attachment_embeds",
            "find_orphan_attachments",
            "move_attachment",
        }
        assert expected == tool_names

    def test_app_rejects_bad_accept_header(self, http_server_module):
        """Server should reject requests without application/json Accept."""
        app = http_server_module.mcp.streamable_http_app()
        with TestClient(app) as client:
            resp = client.post("/mcp", json=_INIT_REQUEST)
        assert resp.status_code == 406

    def test_custom_host_port_applied(self, http_server_module):
        """Host/port from env vars should be on the settings (not passed to run)."""
        assert http_server_module.mcp.settings.host == "0.0.0.0"
        assert http_server_module.mcp.settings.port == 8080


# ── Attachment tools ─────────────────────────────────────────────


async def test_get_file_info_tool_formats_metadata():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.models import FileInfo

    fake = AsyncMock()
    fake.get_file_info.return_value = FileInfo(
        path="Notes/a.txt",
        size=12,
        is_binary=False,
        content_type="text/plain",
        chunk_count=2,
        ctime=1,
        mtime=2,
        inline_cost_bytes=12,
        fits_inline=True,
    )
    with patch.object(srv, "_get_client", return_value=fake):
        result = await srv.get_file_info("Notes/a.txt", inline_budget_bytes=20)

    fake.get_file_info.assert_awaited_once_with("Notes/a.txt", inline_budget_bytes=20)
    assert "path: Notes/a.txt" in result
    assert "inline_cost_bytes: 12" in result
    assert "fits_inline: True" in result
    assert "tools: read_note, read_note_range, get_attachment, create_download_url" in result


async def test_read_note_range_tool_formats_range():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.models import NoteRange

    fake = AsyncMock()
    fake.read_note_range.return_value = NoteRange(
        path="Notes/a.md",
        content="hello",
        offset=0,
        length=5,
        next_offset=5,
        eof=False,
        total_chars=20,
    )
    with patch.object(srv, "_get_client", return_value=fake):
        result = await srv.read_note_range("Notes/a.md", offset=0, length=5)

    fake.read_note_range.assert_awaited_once_with("Notes/a.md", offset=0, length=5)
    assert "Notes/a.md chars 0-5 of 20" in result
    assert result.endswith("\nhello")


async def test_read_note_tool_size_guard_uses_file_info_before_full_read():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.models import FileInfo

    fake = AsyncMock()
    fake.get_file_info.return_value = FileInfo(
        path="Notes/big.md",
        size=2_000_000,
        is_binary=False,
        content_type="text/markdown",
        chunk_count=10,
        ctime=1,
        mtime=2,
        inline_cost_bytes=2_000_000,
    )
    with patch.object(srv, "_get_client", return_value=fake):
        result = await srv.read_note("Notes/big.md", max_bytes=1_000_000)

    fake.read_note.assert_not_awaited()
    assert "read_note_range" in result
    assert "max_bytes=1000000" in result


async def test_read_note_tool_binary_guidance_lists_transfer_tool():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.models import FileInfo

    fake = AsyncMock()
    fake.get_file_info.return_value = FileInfo(
        path="Attachments/pic.png",
        size=20,
        is_binary=True,
        content_type="image/png",
        chunk_count=1,
        ctime=1,
        mtime=2,
        inline_cost_bytes=28,
    )
    with patch.object(srv, "_get_client", return_value=fake):
        result = await srv.read_note("Attachments/pic.png")

    fake.read_note.assert_not_awaited()
    assert "Use get_attachment" in result
    assert "create_download_url" in result


async def test_get_attachment_range_tool_formats_base64():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.models import AttachmentRange

    fake = AsyncMock()
    fake.get_attachment_range.return_value = AttachmentRange(
        path="img/a.png",
        data=b"abc",
        offset=0,
        length=3,
        next_offset=3,
        eof=True,
        total_bytes=3,
        content_type="image/png",
    )
    with patch.object(srv, "_get_client", return_value=fake):
        result = await srv.get_attachment_range("img/a.png", offset=0, length=3)

    fake.get_attachment_range.assert_awaited_once_with(
        "img/a.png",
        offset=0,
        length=3,
        max_bytes=65_536,
    )
    assert "img/a.png bytes 0-3 of 3" in result
    assert "base64:" in result
    assert "YWJj" in result


async def test_create_download_url_tool_requires_streamable_http():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv

    with (
        patch.object(srv, "_transport", "stdio"),
        patch.object(srv, "_get_client", return_value=AsyncMock()),
    ):
        result = await srv.create_download_url("img/a.png")

    assert "not available over stdio" in result


async def test_create_download_url_tool_mints_capability_url():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.links import EphemeralLinkStore
    from obsidian_livesync_mcp.models import FileInfo

    fake = AsyncMock()
    fake.config.link_ttl_seconds = 300
    fake.get_file_info.return_value = FileInfo(
        path="img/a.png",
        size=3,
        is_binary=True,
        content_type="image/png",
        chunk_count=1,
        ctime=1,
        mtime=2,
        inline_cost_bytes=4,
    )
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "download-token")
    with (
        patch.object(srv, "_transport", "streamable-http"),
        patch.object(srv, "_resource_url", "https://mcp.example"),
        patch.object(srv, "_link_store", store),
        patch.object(srv, "_get_client", return_value=fake),
    ):
        result = await srv.create_download_url("img/a.png")

    assert "https://mcp.example/download/download-token" in result
    assert "curl -L -o a.png" in result
    record, status = store.resolve("download-token", mode="download")
    assert status == "ok"
    assert record is not None
    assert record.vault_path == "img/a.png"


async def test_create_download_url_tool_shell_quotes_output_filename():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.links import EphemeralLinkStore
    from obsidian_livesync_mcp.models import FileInfo

    fake = AsyncMock()
    fake.config.link_ttl_seconds = 300
    fake.get_file_info.return_value = FileInfo(
        path="img/bad;touch owned.png",
        size=3,
        is_binary=True,
        content_type="image/png",
        chunk_count=1,
        ctime=1,
        mtime=2,
        inline_cost_bytes=4,
    )
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "download-token")
    with (
        patch.object(srv, "_transport", "streamable-http"),
        patch.object(srv, "_resource_url", "https://mcp.example"),
        patch.object(srv, "_link_store", store),
        patch.object(srv, "_get_client", return_value=fake),
    ):
        result = await srv.create_download_url("img/bad;touch owned.png")

    assert "curl -L -o 'bad;touch owned.png'" in result


async def test_create_upload_url_tool_mints_capability_url_with_global_cap():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.links import EphemeralLinkStore

    fake = AsyncMock()
    fake.config.link_ttl_seconds = 300
    fake.config.max_upload_bytes = 10
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "upload-token")
    with (
        patch.object(srv, "_transport", "streamable-http"),
        patch.object(srv, "_resource_url", "https://mcp.example/"),
        patch.object(srv, "_link_store", store),
        patch.object(srv, "_get_client", return_value=fake),
    ):
        result = await srv.create_upload_url("img/a.png", max_bytes=99)

    assert "https://mcp.example/upload/upload-token" in result
    assert "curl -X PUT --data-binary @FILE" in result
    record, status = store.resolve("upload-token", mode="upload")
    assert status == "ok"
    assert record is not None
    assert record.max_bytes == 10


async def test_add_attachment_tool_decodes_base64():
    import base64
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv

    fake = AsyncMock()
    with patch.object(srv, "_get_client", return_value=fake):
        result = await srv.add_attachment("img/a.png", base64.b64encode(b"hello").decode("ascii"))

    fake.write_attachment.assert_awaited_once_with("img/a.png", b"hello")
    assert "Added attachment: img/a.png" in result


async def test_add_attachment_tool_rejects_bad_base64():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv

    with patch.object(srv, "_get_client", return_value=AsyncMock()):
        result = await srv.add_attachment("img/a.png", "not!base64!")

    assert "Invalid base64" in result


async def test_add_attachment_tool_rejects_plain_text_livesync_path():
    import base64
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv

    fake = AsyncMock()
    fake.write_attachment.side_effect = ValueError(
        "add_attachment only supports binary attachments; use note tools for "
        "plain-text LiveSync files such as .svg"
    )
    with patch.object(srv, "_get_client", return_value=fake):
        result = await srv.add_attachment(
            "img/diagram.svg",
            base64.b64encode(b"<svg/>").decode("ascii"),
        )

    assert result.startswith("Error: add_attachment only supports binary attachments")


async def test_get_attachment_tool_size_guard():
    from unittest.mock import AsyncMock, patch

    import obsidian_livesync_mcp.server as srv
    from obsidian_livesync_mcp.models import AttachmentMetadata

    fake = AsyncMock()
    fake.get_attachment_metadata.return_value = AttachmentMetadata(
        path="img/a.png",
        size=100,
        ctime=1,
        mtime=2,
        extension="png",
        chunk_count=1,
    )
    with patch.object(srv, "_get_client", return_value=fake):
        result = await srv.get_attachment("img/a.png", max_bytes=10)

    fake.read_attachment.assert_not_awaited()
    assert "max_bytes" in result
    assert "image/png" in result
