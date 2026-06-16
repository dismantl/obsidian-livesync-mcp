"""Tests for out-of-band attachment transfer HTTP routes."""

import asyncio
import base64

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from obsidian_livesync_mcp.http_routes import handle_download, handle_upload
from obsidian_livesync_mcp.links import EphemeralLinkStore


class _FakeTransferClient:
    def __init__(self):
        self.docs = {}
        self.chunks = {}
        self.written_attachments = []
        self.written_notes = []
        self.chunk_observations = []
        self.on_chunk = None

    async def _get_doc(self, path: str):
        return self.docs.get(path)

    async def _iter_chunk_data(self, chunk_ids: list[str], batch_size: int = 64):
        for chunk_id in chunk_ids:
            if self.on_chunk is not None:
                self.chunk_observations.append(self.on_chunk())
            yield chunk_id, self.chunks[chunk_id]

    async def write_attachment(self, path: str, data: bytes):
        self.written_attachments.append((path, data))
        return True

    async def write_note(self, path: str, content: str):
        self.written_notes.append((path, content))
        return True


def _app(fake: _FakeTransferClient, store: EphemeralLinkStore) -> Starlette:
    async def download(request):
        return await handle_download(request, fake, store)

    async def upload(request):
        return await handle_upload(request, fake, store)

    return Starlette(
        routes=[
            Route("/download/{token}", download, methods=["GET"]),
            Route("/upload/{token}", upload, methods=["PUT"]),
        ]
    )


def test_download_streams_binary_chunks():
    fake = _FakeTransferClient()
    fake.docs["Attachments/a.bin"] = {
        "_id": "attachments/a.bin",
        "path": "Attachments/a.bin",
        "type": "newnote",
        "children": ["h:a", "h:b"],
        "size": 6,
    }
    fake.chunks = {
        "h:a": base64.b64encode(b"abc").decode("ascii"),
        "h:b": base64.b64encode(b"def").decode("ascii"),
    }
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "download-token")
    store.create("Attachments/a.bin", mode="download", ttl_seconds=60)

    with TestClient(_app(fake, store)) as client:
        response = client.get("/download/download-token")

    assert response.status_code == 200
    assert response.content == b"abcdef"
    assert response.headers["content-type"] == "application/octet-stream"
    assert "attachment" in response.headers["content-disposition"]


def test_download_content_disposition_handles_unicode_and_quotes():
    fake = _FakeTransferClient()
    fake.docs['Attachments/report "q" 📄.txt'] = {
        "_id": 'attachments/report "q" 📄.txt',
        "path": 'Attachments/report "q" 📄.txt',
        "type": "plain",
        "children": ["h:a"],
        "size": 5,
    }
    fake.chunks = {"h:a": "hello"}
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "download-token")
    store.create('Attachments/report "q" 📄.txt', mode="download", ttl_seconds=60)

    with TestClient(_app(fake, store)) as client:
        response = client.get("/download/download-token")

    assert response.status_code == 200
    header = response.headers["content-disposition"]
    assert r'filename="report \"q\" .txt"' in header
    assert "filename*=UTF-8''report%20%22q%22%20%F0%9F%93%84.txt" in header


def test_download_holds_transfer_semaphore_while_streaming(monkeypatch):
    import obsidian_livesync_mcp.http_routes as routes

    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(routes, "_TRANSFER_SEMAPHORE", semaphore)
    fake = _FakeTransferClient()
    fake.on_chunk = semaphore.locked
    fake.docs["Attachments/a.bin"] = {
        "_id": "attachments/a.bin",
        "path": "Attachments/a.bin",
        "type": "newnote",
        "children": ["h:a", "h:b"],
        "size": 6,
    }
    fake.chunks = {
        "h:a": base64.b64encode(b"abc").decode("ascii"),
        "h:b": base64.b64encode(b"def").decode("ascii"),
    }
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "download-token")
    store.create("Attachments/a.bin", mode="download", ttl_seconds=60)

    with TestClient(_app(fake, store)) as client:
        response = client.get("/download/download-token")

    assert response.status_code == 200
    assert response.content == b"abcdef"
    assert fake.chunk_observations == [True, True]


def test_download_streams_text_chunks_as_utf8():
    fake = _FakeTransferClient()
    fake.docs["Notes/a.md"] = {
        "_id": "notes/a.md",
        "path": "Notes/a.md",
        "type": "plain",
        "children": ["h:a", "h:b"],
        "size": 12,
    }
    fake.chunks = {"h:a": "hello ", "h:b": "there"}
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "download-token")
    store.create("Notes/a.md", mode="download", ttl_seconds=60)

    with TestClient(_app(fake, store)) as client:
        response = client.get("/download/download-token")

    assert response.status_code == 200
    assert response.content == b"hello there"
    assert response.headers["content-type"].startswith("text/markdown")


def test_expired_download_token_returns_410():
    fake = _FakeTransferClient()
    now = 1000.0
    store = EphemeralLinkStore(now=lambda: now, token_factory=lambda: "expired-token")
    store.create("Notes/a.md", mode="download", ttl_seconds=1)
    now = 1002.0

    with TestClient(_app(fake, store)) as client:
        response = client.get("/download/expired-token")

    assert response.status_code == 410


def test_invalid_download_token_returns_404():
    fake = _FakeTransferClient()
    store = EphemeralLinkStore(now=lambda: 1000.0)

    with TestClient(_app(fake, store)) as client:
        response = client.get("/download/nope")

    assert response.status_code == 404


def test_upload_writes_binary_and_consumes_token():
    fake = _FakeTransferClient()
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "upload-token")
    store.create("Attachments/a.bin", mode="upload", ttl_seconds=60, max_bytes=10)

    with TestClient(_app(fake, store)) as client:
        first = client.put("/upload/upload-token", content=b"abc")
        second = client.put("/upload/upload-token", content=b"abc")

    assert first.status_code == 200
    assert second.status_code == 404
    assert fake.written_attachments == [("Attachments/a.bin", b"abc")]


def test_upload_routes_plain_text_paths_to_note_writer():
    fake = _FakeTransferClient()
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "upload-token")
    store.create("Notes/a.md", mode="upload", ttl_seconds=60, max_bytes=20)

    with TestClient(_app(fake, store)) as client:
        response = client.put("/upload/upload-token", content="hello".encode("utf-8"))

    assert response.status_code == 200
    assert fake.written_notes == [("Notes/a.md", "hello")]


def test_upload_invalid_utf8_to_text_path_returns_400_without_writing():
    fake = _FakeTransferClient()
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "upload-token")
    store.create("Notes/a.md", mode="upload", ttl_seconds=60, max_bytes=20)

    with TestClient(_app(fake, store), raise_server_exceptions=False) as client:
        response = client.put("/upload/upload-token", content=b"\xff")
        retry = client.put("/upload/upload-token", content=b"hello")

    assert response.status_code == 400
    assert retry.status_code == 404
    assert fake.written_notes == []
    assert fake.written_attachments == []


def test_oversized_upload_returns_413():
    fake = _FakeTransferClient()
    store = EphemeralLinkStore(now=lambda: 1000.0, token_factory=lambda: "upload-token")
    store.create("Attachments/a.bin", mode="upload", ttl_seconds=60, max_bytes=2)

    with TestClient(_app(fake, store)) as client:
        response = client.put("/upload/upload-token", content=b"abc")

    assert response.status_code == 413
    assert fake.written_attachments == []
