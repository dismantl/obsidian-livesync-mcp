"""Data models for vault operations."""

import base64
from dataclasses import dataclass, field


@dataclass
class NoteMetadata:
    path: str
    size: int
    ctime: int  # milliseconds
    mtime: int  # milliseconds
    doc_type: str  # "plain" or "newnote"
    chunk_count: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "ctime": self.ctime,
            "mtime": self.mtime,
            "type": self.doc_type,
            "chunks": self.chunk_count,
        }


@dataclass
class NoteContent:
    path: str
    content: str
    size: int
    is_binary: bool = False

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "content": self.content,
            "size": self.size,
            "is_binary": self.is_binary,
        }


@dataclass
class NoteRange:
    path: str
    content: str
    offset: int
    length: int
    next_offset: int
    eof: bool
    total_chars: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "content": self.content,
            "offset": self.offset,
            "length": self.length,
            "next_offset": self.next_offset,
            "eof": self.eof,
            "total_chars": self.total_chars,
        }


@dataclass
class FileInfo:
    path: str
    size: int
    is_binary: bool
    content_type: str
    chunk_count: int
    ctime: int
    mtime: int
    inline_cost_bytes: int
    fits_inline: bool | None = None

    def to_dict(self) -> dict:
        result = {
            "path": self.path,
            "size": self.size,
            "is_binary": self.is_binary,
            "content_type": self.content_type,
            "chunks": self.chunk_count,
            "ctime": self.ctime,
            "mtime": self.mtime,
            "inline_cost_bytes": self.inline_cost_bytes,
        }
        if self.fits_inline is not None:
            result["fits_inline"] = self.fits_inline
        return result


@dataclass
class AttachmentMetadata:
    path: str
    size: int
    ctime: int  # milliseconds
    mtime: int  # milliseconds
    extension: str
    chunk_count: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "size": self.size,
            "ctime": self.ctime,
            "mtime": self.mtime,
            "extension": self.extension,
            "chunks": self.chunk_count,
        }


@dataclass
class AttachmentContent:
    path: str
    data: bytes
    size: int
    content_type: str

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "data_base64": base64.b64encode(self.data).decode("ascii"),
            "size": self.size,
            "content_type": self.content_type,
        }


@dataclass
class SearchResult:
    path: str
    matches: int
    snippets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "matches": self.matches,
            "snippets": self.snippets,
        }


@dataclass
class BacklinkInfo:
    source_path: str
    context: str  # surrounding text snippet

    def to_dict(self) -> dict:
        return {"source_path": self.source_path, "context": self.context}


@dataclass
class FolderInfo:
    path: str
    note_count: int

    def to_dict(self) -> dict:
        return {"path": self.path, "notes": self.note_count}
