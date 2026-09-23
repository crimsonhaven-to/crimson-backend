"""API keys: machine credentials for the movie-web bridge, not user accounts.

An admin mints a key and hands it to the movie-web fork's proxy, which injects
it server-side when calling the ``/mw`` endpoints, so it never reaches a
browser. The login wall accepts these keys on ``/mw`` paths only, so a key is
not a skeleton key for the rest of the backend.

Like sessions, only the SHA-256 of a key is stored and the raw key is shown
once, at creation. A short non-secret ``key_prefix`` lets the dashboard tell
keys apart without the secret.
"""

import hashlib
import secrets
from typing import Dict, List, Optional, Tuple

from core.clock import utc_now_iso
from core.db_pool import get_connection, lock_schema_init

# The scheme prefix makes a leaked key obvious to secret scanners and keeps it
# apart from session tokens.
KEY_SCHEME = "crimson_mw_"
_PREFIX_LEN = len(KEY_SCHEME) + 6
_LIST_LIMIT = 200


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _public_row(row: dict) -> dict:
    """``id`` is the key hash: irreversible, so safe as the revocation handle."""
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


class ApiKeyStore:
    def init_db(self) -> None:
        with get_connection() as conn:
            lock_schema_init(conn)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS api_keys (
                    key_hash     TEXT PRIMARY KEY,
                    key_prefix   TEXT NOT NULL,
                    label        TEXT,
                    created_by   TEXT,
                    created_at   TEXT NOT NULL,
                    last_used_at TEXT,
                    revoked_at   TEXT                 -- NULL = active
                );
                CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(revoked_at);
                """
            )

    def create_key(self, label: Optional[str], created_by: Optional[str]) -> Tuple[str, dict]:
        """Returns ``(raw_key, public_row)``. This is the only time the raw key exists."""
        raw = KEY_SCHEME + secrets.token_urlsafe(32)
        with get_connection() as conn:
            row = conn.execute(
                """
                INSERT INTO api_keys (key_hash, key_prefix, label, created_by, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (_hash_key(raw), raw[:_PREFIX_LEN], (label or None), created_by, utc_now_iso()),
            ).fetchone()
        return raw, _public_row(dict(row))

    def list_keys(self, include_revoked: bool = True) -> List[Dict]:
        where = "" if include_revoked else "WHERE revoked_at IS NULL"
        with get_connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM api_keys {where} ORDER BY created_at DESC LIMIT %s",
                (_LIST_LIMIT,),
            ).fetchall()
        return [_public_row(dict(r)) for r in rows]

    def revoke_key(self, key_hash: str) -> bool:
        """Soft revoke: the row stays as the audit trail. False if unknown or
        already revoked."""
        if not key_hash:
            return False
        with get_connection() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET revoked_at = %s"
                " WHERE key_hash = %s AND revoked_at IS NULL",
                (utc_now_iso(), key_hash),
            )
            return cur.rowcount > 0

    def validate_and_touch(self, raw_key: str) -> bool:
        """True iff ``raw_key`` is a live key, stamping last_used_at. Called only
        on a login-wall cache miss, so it writes at most once per key per TTL."""
        if not raw_key or not raw_key.startswith(KEY_SCHEME):
            return False
        with get_connection() as conn:
            cur = conn.execute(
                "UPDATE api_keys SET last_used_at = %s"
                " WHERE key_hash = %s AND revoked_at IS NULL",
                (utc_now_iso(), _hash_key(raw_key)),
            )
            return cur.rowcount > 0


store = ApiKeyStore()
