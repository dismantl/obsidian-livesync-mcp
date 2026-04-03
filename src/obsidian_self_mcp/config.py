"""Configuration from environment variables with sensible defaults."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    couch_url: str = os.environ.get("OBSIDIAN_COUCH_URL", "") or os.environ.get("COUCHDB_URL", "")
    couch_user: str = os.environ.get("OBSIDIAN_COUCH_USER", "") or os.environ.get(
        "COUCHDB_USER", ""
    )
    couch_pass: str = os.environ.get("OBSIDIAN_COUCH_PASS", "") or os.environ.get(
        "COUCHDB_PASSWORD", ""
    )
    db_name: str = (
        os.environ.get("OBSIDIAN_COUCH_DB", "")
        or os.environ.get("COUCHDB_DB", "")
        or os.environ.get("COUCHDB_DATABASE", "obsidian-vault")
    )

    # LiveSync path obfuscation passphrase (optional — set to match your
    # LiveSync client's passphrase when usePathObfuscation is enabled)
    obfuscate_passphrase: str | None = os.environ.get("OBSIDIAN_OBFUSCATE_PASSPHRASE") or None

    # OAuth/OIDC configuration (optional — OAuth is opt-in, but all fields
    # are required once OAUTH_ISSUER_URL is set)
    oauth_issuer_url: str | None = os.environ.get("OAUTH_ISSUER_URL") or None
    oauth_client_id: str | None = os.environ.get("OAUTH_CLIENT_ID") or None
    oauth_client_secret: str | None = os.environ.get("OAUTH_CLIENT_SECRET") or None
    oauth_authorized_email: str | None = os.environ.get("OAUTH_AUTHORIZED_EMAIL") or None

    def __post_init__(self):
        if not self.couch_url:
            raise ValueError("CouchDB URL is required. Set OBSIDIAN_COUCH_URL or COUCHDB_URL.")
        if self.oauth_issuer_url:
            if not self.oauth_client_id:
                raise ValueError("OAUTH_CLIENT_ID is required when OAUTH_ISSUER_URL is set.")
            if not self.oauth_client_secret:
                raise ValueError("OAUTH_CLIENT_SECRET is required when OAUTH_ISSUER_URL is set.")
            if not self.oauth_authorized_email:
                raise ValueError(
                    "OAUTH_AUTHORIZED_EMAIL is required when OAUTH_ISSUER_URL is set. "
                    "Without it, any user who can authenticate with the OIDC provider "
                    "would have full vault access."
                )

    def __repr__(self) -> str:
        return (
            f"Config(couch_url={self.couch_url!r}, couch_user={self.couch_user!r}, "
            f"couch_pass='***', db_name={self.db_name!r}, "
            f"oauth_issuer_url={self.oauth_issuer_url!r}, "
            f"oauth_client_secret='***')"
        )

    @property
    def db_url(self) -> str:
        return f"{self.couch_url}/{self.db_name}"

    @property
    def oauth_enabled(self) -> bool:
        return self.oauth_issuer_url is not None

    @property
    def oauth_callback_path(self) -> str:
        return "/oauth/callback"
