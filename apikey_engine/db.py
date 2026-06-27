"""
API-key storage (PostgreSQL) — machine credentials for the movie-web bridge.

These keys are NOT user accounts. They are minted by an admin (see
account_engine.admin_routes -> /admin/api-keys) and handed to the modified
movie-web fork's proxy so it can call the ``/mw`` bridge endpoints on the
viewer's behalf. The key is injected server-side at the fork's proxy and never
reaches the browser; see the project notes on the API-key bridge.

Two deliberate properties mirror how sessions are stored (account_engine.db):

  * only the SHA-256 *hash* of a key is stored, so a DB leak exposes no usable
    credential — the raw key is shown to the admin exactly once, at creation;
  * the raw key carries a human-readable scheme prefix (``crimson_mw_``) and we
    persist a short, non-secret ``key_prefix`` so the dashboard can identify a
    key in a list without ever seeing the secret again.

The login wall (api.py) checks these keys ONLY for ``/mw`` paths, so a key is
scoped to the bridge and is not a skeleton key for the rest of the backend.

Storage is the shared pool (db_pool.get_connection); a Fribb mapping resync only
DELETEs the mapping tables, so these rows are never touched by a sync.
"""

import hashlib
import secrets
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from core.db_pool import get_connection, lock_schema_init

# Raw keys look like ``crimson_mw_<43 url-safe chars>``. The scheme prefix makes
# a leaked key obvious in logs/secret-scanners and namespaces it away from
# session tokens; the body is 32 random bytes (token_urlsafe(32)).
KEY_SCHEME = "crimson_mw_"
_PREFIX_LEN = len(KEY_SCHEME) + 6  # what we keep for display, e.g. crimson_mw_AbC1de


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ApiKeyStore:
    """Thin PostgreSQL data layer for movie-web bridge API keys.

    Synchronous psycopg calls borrowing from the shared pool, matching the rest
    of the backend (api.py calls these from its async handlers via the thread
    pool). Timestamps are ISO-8601 TEXT, consistent with account_engine.
    """

    def __init__(self, db_path: Optional[str] = None):
        # Retained for call-site symmetry with the other stores; ignored (storage
        # is the shared pool configured via DATABASE_URL).
        self._explicit_path = db_path

    # -- connection / schema --------------------------------------------
    def _connect(self):
        return get_connection()

    def init_db(self) -> None:
        """Create the schema (idempotent)."""
        with self._connect() as conn:
            lock_schema_init(conn)  # serialize DDL across replicas (see db_pool)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash     TEXT PRIMARY KEY,   -- SHA-256 of the raw key
                    key_prefix   TEXT NOT NULL,      -- non-secret display fragment
                    label        TEXT,               -- admin-set note ("movie-web prod")
                    created_by   TEXT,               -- admin email/id that minted it
                    created_at   TEXT NOT NULL,
                    last_used_at TEXT,               -- touched on validate (rate-limited by cache)
                    revoked_at   TEXT                 -- NULL = active
                );
                CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(revoked_at);
                """
            )

    # -- minting / listing / revoking (admin dashboard) -----------------
    @staticmethod
    def _public_row(row: dict) -> dict:
        """Shape a stored row for the dashboard. ``id`` is the key_hash — safe to
        expose (irreversible, useless without the raw key) and used as the handle
        for revocation. The raw key is NEVER returned here; it exists only in the
        create response."""
        return {
            "id": row["key_hash"],
            "key_prefix": row["key_prefix"],
            "label": row.get("label"),
            "created_by": row.get("created_by"),
            "created_at": row.get("created_at"),
            "last_used_at": row.get("last_used_at"),
            "revoked": row.get("revoked_at") is not None,
            "revoked_at": row.get("revoked_at"),
        }

    def create_key(self, label: Optional[str], created_by: Optional[str]) -> Tuple[str, dict]:
        """Mint a fresh key. Returns ``(raw_key, public_row)``; the raw key is the
        ONLY time the secret is available — store only its hash."""
        raw = KEY_SCHEME + secrets.token_urlsafe(32)
        prefix = raw[:_PREFIX_LEN]
        now = _iso(_now())
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO api_keys (key_hash, key_prefix, label, created_by, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (_hash_key(raw), prefix, (label or None), created_by, now),
            ).fetchone()
        return raw, self._public_row(dict(row))

    def list_keys(self, include_revoked: bool = True, limit: int = 200) -> List[Dict]:
        """Most-recently-minted keys for the dashboard (never the raw secret)."""
        with self._connect() as conn:
            if include_revoked:
                rows = conn.execute(
                    "SELECT * FROM api_keys ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM api_keys WHERE revoked_at IS NULL"
                    " ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                ).fetchall()
        return [self._public_row(dict(r)) for r in rows]

    def revoke_key(self, key_hash: str) -> bool:
        """Soft-revoke a key by its id (key_hash). Kept as a row (audit trail);
        a revoked key fails validation immediately. Returns False if unknown or
        already revoked."""
        if not key_hash:
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = %s"
                " WHERE key_hash = %s AND revoked_at IS NULL",
                (_iso(_now()), key_hash),
            )
            return cur.rowcount > 0

    # -- validation (login wall, /mw only) ------------------------------
    def validate_and_touch(self, raw_key: str) -> bool:
        """True iff ``raw_key`` is a known, non-revoked key. On success, stamp
        last_used_at. Called only on a cache miss in the wall (see api.py), so the
        write happens at most once per key per cache-TTL, not per request."""
        if not raw_key or not raw_key.startswith(KEY_SCHEME):
            return False
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET last_used_at = %s"
                " WHERE key_hash = %s AND revoked_at IS NULL",
                (_iso(_now()), _hash_key(raw_key)),
            )
            return cur.rowcount > 0
