"""Data models for vault operations."""

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
