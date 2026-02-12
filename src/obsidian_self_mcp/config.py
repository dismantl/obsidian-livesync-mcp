"""Configuration from environment variables with sensible defaults."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    couch_url: str = os.environ.get("OBSIDIAN_COUCH_URL", "")
    couch_user: str = os.environ.get("OBSIDIAN_COUCH_USER", "")
    couch_pass: str = os.environ.get("OBSIDIAN_COUCH_PASS", "")
    db_name: str = os.environ.get("OBSIDIAN_COUCH_DB", "obsidian-vault")

    @property
    def db_url(self) -> str:
        return f"{self.couch_url}/{self.db_name}"
