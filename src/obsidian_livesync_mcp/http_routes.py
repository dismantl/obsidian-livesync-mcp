"""Custom HTTP routes for out-of-band vault file transfer."""

import asyncio
import base64
import mimetypes
import os
from pathlib import PurePosixPath
from urllib.parse import quote

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from .attachments import _is_livesync_plain_text_path
from .links import EphemeralLinkStore, ResolveStatus

MAX_CONCURRENT_TRANSFERS = int(os.environ.get("MCP_MAX_CONCURRENT_TRANSFERS", "2"))
_TRANSFER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_TRANSFERS)


async def handle_download(request: Request, client, store: EphemeralLinkStore):
    """Serve a token-bound vault file without sending bytes through MCP."""
    token = request.path_params["token"]
    record, status = store.resolve(token, mode="download")
    _raise_for_status(status)

    assert record is not None
    doc = await client._get_doc(record.vault_path)
    if not doc or doc.get("deleted"):
        raise HTTPException(status_code=404)

    path = doc.get("path", record.vault_path)
    content_type = mimetypes.guess_type(path)[0]
    if content_type is None:
        content_type = "application/octet-stream" if doc.get("type") == "newnote" else "text/plain"

    return StreamingResponse(
        _iter_limited_doc_bytes(client, doc),
        media_type=content_type,
        headers={"Content-Disposition": _content_disposition(path)},
    )


async def handle_upload(request: Request, client, store: EphemeralLinkStore):
    """Consume an upload token and write the raw request body to the vault path."""
    token = request.path_params["token"]
    record, status = store.resolve(token, mode="upload", consume=True)
    _raise_for_status(status)

    assert record is not None
    async with _TRANSFER_SEMAPHORE:
        data = bytearray()
        max_bytes = record.max_bytes
        async for chunk in request.stream():
            data.extend(chunk)
            if max_bytes is not None and len(data) > max_bytes:
                raise HTTPException(status_code=413, detail="Upload exceeds max_bytes")

        raw = bytes(data)
        if _is_livesync_plain_text_path(record.vault_path):
            await client.write_note(record.vault_path, raw.decode("utf-8"))
        else:
            await client.write_attachment(record.vault_path, raw)

    return JSONResponse({"ok": True, "path": record.vault_path, "size": len(raw)})


async def _iter_doc_bytes(client, doc: dict):
    if doc.get("type") == "notes":
        data = doc.get("data", "")
        text = "".join(data) if isinstance(data, list) else str(data)
        yield text.encode("utf-8")
        return

    chunk_ids = doc.get("children", [])
    if doc.get("type") == "newnote":
        async for _, data in client._iter_chunk_data(chunk_ids):
            yield base64.b64decode(data)
    else:
        async for _, data in client._iter_chunk_data(chunk_ids):
            yield data.encode("utf-8")


async def _iter_limited_doc_bytes(client, doc: dict):
    async with _TRANSFER_SEMAPHORE:
        async for chunk in _iter_doc_bytes(client, doc):
            yield chunk


def _raise_for_status(status: ResolveStatus) -> None:
    if status == "ok":
        return
    if status == "expired":
        raise HTTPException(status_code=410)
    raise HTTPException(status_code=404)


def _download_name(path: str) -> str:
    name = PurePosixPath(path).name
    return name or "download"


def _content_disposition(path: str) -> str:
    filename = _download_name(path)
    fallback = filename.encode("ascii", "ignore").decode("ascii")
    fallback = "".join(ch for ch in fallback if 32 <= ord(ch) < 127).strip()
    if not fallback:
        fallback = "download"
    fallback = fallback.replace("\\", "\\\\").replace('"', '\\"')
    encoded = quote(filename, safe="")
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"
