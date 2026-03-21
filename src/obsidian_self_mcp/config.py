"""Configuration from environment variables with sensible defaults."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    couch_url: str = (
        os.environ.get("OBSIDIAN_COUCH_URL", "") or os.environ.get("COUCHDB_URL", "")
    )
    couch_user: str = (
        os.environ.get("OBSIDIAN_COUCH_USER", "") or os.environ.get("COUCHDB_USER", "")
    )
    couch_pass: str = (
        os.environ.get("OBSIDIAN_COUCH_PASS", "") or os.environ.get("COUCHDB_PASSWORD", "")
    )
    db_name: str = (
        os.environ.get("OBSIDIAN_COUCH_DB", "") or os.environ.get("COUCHDB_DB", "obsidian-vault")
    )

    def __post_init__(self):
        if not self.couch_url:
            raise ValueError(
                "CouchDB URL is required. Set OBSIDIAN_COUCH_URL or COUCHDB_URL."
            )

    def __repr__(self) -> str:
        return (
            f"Config(couch_url={self.couch_url!r}, couch_user={self.couch_user!r}, "
            f"couch_pass='***', db_name={self.db_name!r})"
        )

    @property
    def db_url(self) -> str:
        return f"{self.couch_url}/{self.db_name}"
