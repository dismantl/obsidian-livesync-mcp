"""CouchDB-backed storage for OAuth clients, access tokens, and refresh tokens."""

import asyncio
import logging
import time

import httpx
from mcp.server.auth.provider import AccessToken, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

logger = logging.getLogger(__name__)

OAUTH_DB_NAME = "mcp_oauth"

DESIGN_DOC = {
    "_id": "_design/oauth",
    "views": {
        "by_expiry": {
            "map": (
                "function(doc) {"
                "  if (doc.expires_at && doc.type !== 'client') {"
                "    emit(doc.expires_at, null);"
                "  }"
                "}"
            ),
        },
        "by_token_pair": {
            "map": (
                "function(doc) {  if (doc.token_pair_id) {    emit(doc.token_pair_id, null);  }}"
            ),
        },
    },
}


class OAuthStore:
    """Persistent storage for OAuth data in CouchDB."""

    def __init__(self, couch_url: str, couch_user: str, couch_pass: str):
        self._base_url = f"{couch_url}/{OAUTH_DB_NAME}"
        self._couch_url = couch_url
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            auth=(couch_user, couch_pass),
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        self._purge_task: asyncio.Task | None = None

    async def ensure_db(self) -> None:
        """Create the OAuth database and design document if they don't exist."""
        # Create database
        resp = await self._client.put(f"{self._couch_url}/{OAUTH_DB_NAME}")
        if resp.status_code not in (201, 412):  # 412 = already exists
            resp.raise_for_status()

        # Create or update design document
        resp = await self._client.get("/_design/oauth")
        if resp.status_code == 200:
            existing = resp.json()
            if existing.get("views") != DESIGN_DOC["views"]:
                doc = {**DESIGN_DOC, "_rev": existing["_rev"]}
                resp = await self._client.put("/_design/oauth", json=doc)
                resp.raise_for_status()
        elif resp.status_code == 404:
            resp = await self._client.put("/_design/oauth", json=DESIGN_DOC)
            resp.raise_for_status()
        else:
            resp.raise_for_status()

    async def close(self) -> None:
        self.stop_purge_task()
        if not self._client.is_closed:
            await self._client.aclose()

    # ── Client CRUD ───────────────────────────────────────────────

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        resp = await self._client.get(f"/client:{client_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        doc = resp.json()
        return self._doc_to_client(doc)

    async def save_client(self, client: OAuthClientInformationFull) -> None:
        doc = client.model_dump(mode="json", exclude_none=True)
        doc["_id"] = f"client:{client.client_id}"
        doc["type"] = "client"
        # Check for existing doc to get _rev
        existing = await self._client.get(f"/client:{client.client_id}")
        if existing.status_code == 200:
            doc["_rev"] = existing.json()["_rev"]
        resp = await self._client.put(f"/client:{client.client_id}", json=doc)
        resp.raise_for_status()

    # ── Access Token CRUD ─────────────────────────────────────────

    async def get_access_token(self, token: str) -> AccessToken | None:
        resp = await self._client.get(f"/access_token:{token}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        doc = resp.json()
        # Lazy expiry check
        if doc.get("expires_at") and doc["expires_at"] < int(time.time()):
            await self._delete_doc(f"access_token:{token}", doc["_rev"])
            return None
        return AccessToken(
            token=doc["token"],
            client_id=doc["client_id"],
            scopes=doc.get("scopes", []),
            expires_at=doc.get("expires_at"),
            resource=doc.get("resource"),
        )

    async def save_access_token(self, token: AccessToken, token_pair_id: str) -> None:
        doc = {
            "_id": f"access_token:{token.token}",
            "type": "access_token",
            "token": token.token,
            "client_id": token.client_id,
            "scopes": token.scopes,
            "expires_at": token.expires_at,
            "resource": token.resource,
            "token_pair_id": token_pair_id,
        }
        resp = await self._client.put(f"/access_token:{token.token}", json=doc)
        resp.raise_for_status()

    # ── Refresh Token CRUD ────────────────────────────────────────

    async def get_refresh_token(self, token: str) -> RefreshToken | None:
        resp = await self._client.get(f"/refresh_token:{token}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        doc = resp.json()
        # Lazy expiry check
        if doc.get("expires_at") and doc["expires_at"] < int(time.time()):
            await self._delete_doc(f"refresh_token:{token}", doc["_rev"])
            return None
        return RefreshToken(
            token=doc["token"],
            client_id=doc["client_id"],
            scopes=doc.get("scopes", []),
            expires_at=doc.get("expires_at"),
        )

    async def save_refresh_token(self, token: RefreshToken, token_pair_id: str) -> None:
        doc = {
            "_id": f"refresh_token:{token.token}",
            "type": "refresh_token",
            "token": token.token,
            "client_id": token.client_id,
            "scopes": token.scopes,
            "expires_at": token.expires_at,
            "token_pair_id": token_pair_id,
        }
        resp = await self._client.put(f"/refresh_token:{token.token}", json=doc)
        resp.raise_for_status()

    # ── Cascading Revocation ──────────────────────────────────────

    async def get_tokens_by_pair_id(self, pair_id: str) -> list[dict]:
        """Query the by_token_pair view to find all tokens sharing a pair ID."""
        resp = await self._client.get(
            "/_design/oauth/_view/by_token_pair",
            params={"key": f'"{pair_id}"', "include_docs": "true"},
        )
        resp.raise_for_status()
        return [row["doc"] for row in resp.json().get("rows", []) if "doc" in row]

    async def delete_token(self, doc_id: str) -> None:
        """Delete a token document by its full _id (e.g., 'access_token:xyz')."""
        resp = await self._client.get(f"/{doc_id}")
        if resp.status_code == 404:
            return
        resp.raise_for_status()
        doc = resp.json()
        await self._delete_doc(doc_id, doc["_rev"])

    async def delete_paired_tokens(self, token_value: str, token_type: str) -> None:
        """Delete the sibling token via token_pair_id lookup.

        Looks up the token doc to find its token_pair_id, then deletes all
        other tokens sharing that pair ID (cascading revocation).
        """
        doc_id = f"{token_type}:{token_value}"
        resp = await self._client.get(f"/{doc_id}")
        if resp.status_code == 404:
            return
        if resp.status_code != 200:
            logger.warning("Failed to look up paired token %s: HTTP %d", doc_id, resp.status_code)
            return
        doc = resp.json()
        pair_id = doc.get("token_pair_id")
        if not pair_id:
            return
        paired_docs = await self.get_tokens_by_pair_id(pair_id)
        for paired_doc in paired_docs:
            if paired_doc["_id"] != doc_id:
                await self.delete_token(paired_doc["_id"])

    # ── Background Purge ──────────────────────────────────────────

    def start_purge_task(self) -> None:
        if self._purge_task is None or self._purge_task.done():
            self._purge_task = asyncio.create_task(self._purge_loop())

    def stop_purge_task(self) -> None:
        if self._purge_task and not self._purge_task.done():
            self._purge_task.cancel()

    async def _purge_loop(self) -> None:
        """Periodically delete expired tokens from CouchDB."""
        while True:
            try:
                await asyncio.sleep(3600)  # hourly
                await self.purge_expired()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in OAuth token purge task")

    async def purge_expired(self) -> int:
        """Delete all expired token documents. Returns count of deleted docs."""
        now = int(time.time())
        resp = await self._client.get(
            "/_design/oauth/_view/by_expiry",
            params={"endkey": str(now), "include_docs": "true"},
        )
        resp.raise_for_status()
        rows = resp.json().get("rows", [])
        deleted = 0
        for row in rows:
            doc = row.get("doc")
            if doc:
                try:
                    await self._delete_doc(doc["_id"], doc["_rev"])
                    deleted += 1
                except Exception:
                    logger.warning("Failed to purge expired doc %s", doc["_id"])
        if deleted:
            logger.info("Purged %d expired OAuth token(s)", deleted)
        return deleted

    # ── Helpers ────────────────────────────────────────────────────

    async def _delete_doc(self, doc_id: str, rev: str) -> None:
        resp = await self._client.delete(f"/{doc_id}", params={"rev": rev})
        if resp.status_code not in (200, 202, 404):
            resp.raise_for_status()

    @staticmethod
    def _doc_to_client(doc: dict) -> OAuthClientInformationFull:
        """Convert a CouchDB document back to an OAuthClientInformationFull."""
        # Remove CouchDB metadata fields before passing to Pydantic
        cleaned = {k: v for k, v in doc.items() if not k.startswith("_") and k != "type"}
        return OAuthClientInformationFull.model_validate(cleaned)
