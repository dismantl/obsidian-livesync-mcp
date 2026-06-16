"""Ephemeral capability-token store for out-of-band transfer URLs."""

import secrets
import time
from dataclasses import dataclass
from typing import Callable, Literal

LinkMode = Literal["download", "upload"]
ResolveStatus = Literal["ok", "missing", "expired", "wrong_mode"]


@dataclass(frozen=True)
class LinkRecord:
    token: str
    vault_path: str
    mode: LinkMode
    expires_at: float
    max_bytes: int | None = None


class EphemeralLinkStore:
    """In-memory store for short-lived transfer capability tokens."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.time,
        token_factory: Callable[[], str] | None = None,
    ):
        self._now = now
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._store: dict[str, LinkRecord] = {}

    def create(
        self,
        vault_path: str,
        *,
        mode: LinkMode,
        ttl_seconds: int,
        max_bytes: int | None = None,
    ) -> LinkRecord:
        self._sweep_expired()
        token = self._new_token()
        record = LinkRecord(
            token=token,
            vault_path=vault_path,
            mode=mode,
            expires_at=self._now() + ttl_seconds,
            max_bytes=max_bytes,
        )
        self._store[token] = record
        return record

    def resolve(
        self,
        token: str,
        *,
        mode: LinkMode,
        consume: bool = False,
    ) -> tuple[LinkRecord | None, ResolveStatus]:
        record = self._store.get(token)
        if record is None:
            return None, "missing"
        if record.expires_at < self._now():
            self._store.pop(token, None)
            return None, "expired"
        if record.mode != mode:
            return None, "wrong_mode"
        if consume:
            self._store.pop(token, None)
        return record, "ok"

    def _new_token(self) -> str:
        while True:
            token = self._token_factory()
            if token not in self._store:
                return token

    def _sweep_expired(self) -> None:
        now = self._now()
        expired = [token for token, record in self._store.items() if record.expires_at < now]
        for token in expired:
            self._store.pop(token, None)
