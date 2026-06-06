"""
Account storage (SQLite) — accounts, sessions, login challenges, favorites and
watch progress.

Kept in its OWN database file (default ``accounts.db``), deliberately separate
from ``anime_mappings.db``: the mapping DB is wiped and rebuilt wholesale on
every Fribb sync (see metadata_engine.db_handler.sync_database_async), which
would take user data with it. User data must outlive syncs, so it lives apart.

Identity model (see account_engine.ed25519 / README): an account *is* an Ed25519
public key. The server stores only that public key — never the mnemonic, never a
password, never the private key. Possession is proven per-login by signing a
one-time challenge. Sessions are opaque random bearer tokens, stored only as a
SHA-256 hash so a DB leak can't be replayed.
"""

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

# Lifetimes.
SESSION_TTL = timedelta(days=30)
CHALLENGE_TTL = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AccountStore:
    """Thin SQLite data layer for the account system.

    Methods are synchronous sqlite calls, matching the rest of this backend
    (api.py calls sqlite synchronously from its async handlers); the volumes are
    tiny and per-request, so this is fine.
    """

    def __init__(self, db_path: Optional[str] = None):
        # Resolve lazily (see db_path) so an ACCOUNTS_DB set in .env is honoured
        # even though api.py constructs this before load_dotenv() runs.
        self._explicit_path = db_path

    @property
    def db_path(self) -> str:
        return self._explicit_path or os.getenv("ACCOUNTS_DB", "accounts.db")

    # -- connection / schema --------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        """Create the schema (idempotent)."""
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    public_key    TEXT UNIQUE NOT NULL,
                    label         TEXT,
                    created_at    TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id    INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES accounts(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

                CREATE TABLE IF NOT EXISTS challenges (
                    challenge  TEXT PRIMARY KEY,
                    public_key TEXT NOT NULL,
                    purpose    TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS favorites (
                    user_id       INTEGER NOT NULL,
                    item_key      TEXT NOT NULL,
                    tmdb_id       INTEGER,
                    anilist_id    INTEGER,
                    season_number INTEGER,
                    media_type    TEXT,
                    title         TEXT,
                    poster        TEXT,
                    added_at      TEXT NOT NULL,
                    PRIMARY KEY (user_id, item_key),
                    FOREIGN KEY (user_id) REFERENCES accounts(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id, added_at);

                CREATE TABLE IF NOT EXISTS watch_progress (
                    user_id          INTEGER NOT NULL,
                    item_key         TEXT NOT NULL,
                    tmdb_id          INTEGER,
                    anilist_id       INTEGER,
                    season_number    INTEGER,
                    episode_number   INTEGER,
                    position_seconds REAL,
                    duration_seconds REAL,
                    status           TEXT NOT NULL DEFAULT 'in_progress',
                    title            TEXT,
                    poster           TEXT,
                    updated_at       TEXT NOT NULL,
                    PRIMARY KEY (user_id, item_key),
                    FOREIGN KEY (user_id) REFERENCES accounts(user_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_progress_user ON watch_progress(user_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_progress_status ON watch_progress(user_id, status);
                """
            )
        self.purge_expired()
        print(f"[AccountStore] Schema ready at '{self.db_path}'.")

    # -- accounts -------------------------------------------------------
    def get_account_by_public_key(self, public_key: str) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE public_key = ?", (public_key,)
            ).fetchone()
            return dict(row) if row else None

    def get_account(self, user_id: int) -> Optional[Dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM accounts WHERE user_id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def create_account(self, public_key: str, label: Optional[str]) -> Dict:
        """Create an account for a public key. Raises sqlite3.IntegrityError if
        the key already exists."""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO accounts (public_key, label, created_at) VALUES (?, ?, ?)",
                (public_key, label, _iso(_now())),
            )
            user_id = cur.lastrowid
        return self.get_account(user_id)

    def touch_login(self, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE accounts SET last_login_at = ? WHERE user_id = ?",
                (_iso(_now()), user_id),
            )

    # -- challenges (one-time login nonces) -----------------------------
    def create_challenge(self, public_key: str, purpose: str) -> Tuple[str, str]:
        challenge = secrets.token_urlsafe(32)
        expires = _now() + CHALLENGE_TTL
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO challenges (challenge, public_key, purpose, expires_at) VALUES (?, ?, ?, ?)",
                (challenge, public_key, purpose, _iso(expires)),
            )
        return challenge, _iso(expires)

    def consume_challenge(self, challenge: str, public_key: str, purpose: str) -> bool:
        """Atomically validate + delete a challenge. True only if it existed for
        this public key + purpose and had not expired (single use)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT public_key, purpose, expires_at FROM challenges WHERE challenge = ?",
                (challenge,),
            ).fetchone()
            if row is None:
                return False
            # Always delete (single-use), even if it turns out invalid/expired.
            conn.execute("DELETE FROM challenges WHERE challenge = ?", (challenge,))
            if row["public_key"] != public_key or row["purpose"] != purpose:
                return False
            try:
                expires = datetime.fromisoformat(row["expires_at"])
            except ValueError:
                return False
            return expires > _now()

    # -- sessions -------------------------------------------------------
    def create_session(self, user_id: int) -> Tuple[str, str]:
        """Issue a session. Returns (raw_token, expires_at_iso); only the hash is
        stored."""
        raw = secrets.token_urlsafe(32)
        expires = _now() + SESSION_TTL
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (_hash_token(raw), user_id, _iso(_now()), _iso(expires)),
            )
        return raw, _iso(expires)

    def get_user_by_session(self, raw_token: str) -> Optional[Dict]:
        if not raw_token:
            return None
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT a.* , s.expires_at AS session_expires_at
                FROM sessions s JOIN accounts a ON a.user_id = s.user_id
                WHERE s.token_hash = ?
                """,
                (_hash_token(raw_token),),
            ).fetchone()
            if row is None:
                return None
            try:
                if datetime.fromisoformat(row["session_expires_at"]) <= _now():
                    conn.execute(
                        "DELETE FROM sessions WHERE token_hash = ?", (_hash_token(raw_token),)
                    )
                    return None
            except ValueError:
                return None
            return dict(row)

    def delete_session(self, raw_token: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (_hash_token(raw_token),)
            )

    # -- favorites ------------------------------------------------------
    def upsert_favorite(self, user_id: int, fav: Dict) -> Dict:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO favorites
                    (user_id, item_key, tmdb_id, anilist_id, season_number,
                     media_type, title, poster, added_at)
                VALUES (:user_id, :item_key, :tmdb_id, :anilist_id, :season_number,
                        :media_type, :title, :poster, :added_at)
                ON CONFLICT(user_id, item_key) DO UPDATE SET
                    tmdb_id=excluded.tmdb_id, anilist_id=excluded.anilist_id,
                    season_number=excluded.season_number, media_type=excluded.media_type,
                    title=excluded.title, poster=excluded.poster
                """,
                {"user_id": user_id, "added_at": _iso(_now()), **fav},
            )
            row = conn.execute(
                "SELECT * FROM favorites WHERE user_id = ? AND item_key = ?",
                (user_id, fav["item_key"]),
            ).fetchone()
            return dict(row)

    def list_favorites(self, user_id: int) -> List[Dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM favorites WHERE user_id = ? ORDER BY added_at DESC",
                (user_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def remove_favorite(self, user_id: int, item_key: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM favorites WHERE user_id = ? AND item_key = ?",
                (user_id, item_key),
            )
            return cur.rowcount > 0

    def is_favorite(self, user_id: int, item_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM favorites WHERE user_id = ? AND item_key = ?",
                (user_id, item_key),
            ).fetchone()
            return row is not None

    # -- watch progress -------------------------------------------------
    def upsert_progress(self, user_id: int, prog: Dict) -> Dict:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watch_progress
                    (user_id, item_key, tmdb_id, anilist_id, season_number,
                     episode_number, position_seconds, duration_seconds, status,
                     title, poster, updated_at)
                VALUES (:user_id, :item_key, :tmdb_id, :anilist_id, :season_number,
                        :episode_number, :position_seconds, :duration_seconds, :status,
                        :title, :poster, :updated_at)
                ON CONFLICT(user_id, item_key) DO UPDATE SET
                    tmdb_id=excluded.tmdb_id, anilist_id=excluded.anilist_id,
                    season_number=excluded.season_number, episode_number=excluded.episode_number,
                    position_seconds=excluded.position_seconds, duration_seconds=excluded.duration_seconds,
                    status=excluded.status, title=excluded.title, poster=excluded.poster,
                    updated_at=excluded.updated_at
                """,
                {"user_id": user_id, "updated_at": _iso(_now()), **prog},
            )
            row = conn.execute(
                "SELECT * FROM watch_progress WHERE user_id = ? AND item_key = ?",
                (user_id, prog["item_key"]),
            ).fetchone()
            return dict(row)

    def list_progress(self, user_id: int, status: Optional[str] = None) -> List[Dict]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM watch_progress WHERE user_id = ? AND status = ? ORDER BY updated_at DESC",
                    (user_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM watch_progress WHERE user_id = ? ORDER BY updated_at DESC",
                    (user_id,),
                ).fetchall()
            return [dict(r) for r in rows]

    def remove_progress(self, user_id: int, item_key: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM watch_progress WHERE user_id = ? AND item_key = ?",
                (user_id, item_key),
            )
            return cur.rowcount > 0

    # -- maintenance ----------------------------------------------------
    def purge_expired(self) -> None:
        """Drop expired sessions + challenges (cheap housekeeping)."""
        now = _iso(_now())
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            conn.execute("DELETE FROM challenges WHERE expires_at <= ?", (now,))
