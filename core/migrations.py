"""Versioned schema migrations from ``migrations/NNN_name.sql``.

The ``init_db()`` functions stay the idempotent baseline and run first; their DDL
is deliberately not transcribed into ``migrations/``, so a fresh database and a
long-lived one take the same path to the same schema. This runner owns every
change after that.

``apply_pending()`` runs once at startup, after every ``init_db()``, in one
transaction under ``SCHEMA_INIT_LOCK``: the transaction-scoped lock makes "read
what is applied, apply the rest" atomic against other booting replicas, and a
failure rolls the whole batch back. So nothing that cannot run in a transaction
(``CREATE INDEX CONCURRENTLY``) belongs in a migration.

Each applied file's SHA-256 is stored and re-checked at boot. A mismatch means
an applied file was edited; it is logged and shown on ``/health`` but not fatal,
because refusing to boot over a whitespace change would turn bookkeeping into an
outage.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from core.db_pool import get_connection, lock_schema_init

logger = logging.getLogger("crimson.migrations")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# A restricted charset so nothing odd reaches a log line.
_FILENAME_RE = re.compile(r"^(\d{3,})_([A-Za-z0-9][A-Za-z0-9._-]*)\.sql$")


class Migration(NamedTuple):
    version: int
    name: str
    filename: str
    sql: str
    checksum: str


def checksum(sql: str) -> str:
    """SHA-256 of the text with CRLF normalised: the repo is edited on Windows
    and built on Linux, and line endings must not read as drift."""
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _is_effectively_empty(sql: str) -> bool:
    """Postgres rejects an empty query string, so a comment-only file is
    recorded rather than executed."""
    body = re.sub(r"--[^\n]*", "", sql)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return not body.strip()


def discover(directory: Optional[Path] = None) -> List[Migration]:
    """Every migration file, ordered by numeric version (so ``1000_`` follows
    ``999_``). Raises ``ValueError`` on a duplicate version."""
    directory = directory or MIGRATIONS_DIR
    if not directory.is_dir():
        return []

    found: List[Migration] = []
    seen: Dict[int, str] = {}
    for path in directory.iterdir():
        if not path.is_file() or path.suffix != ".sql":
            continue
        m = _FILENAME_RE.match(path.name)
        if not m:
            logger.warning(
                "Ignoring %s: migration files must be named NNN_name.sql", path.name
            )
            continue
        version = int(m.group(1))
        if version in seen:
            raise ValueError(
                f"Duplicate migration version {version}: {seen[version]} and {path.name}"
            )
        seen[version] = path.name
        sql = path.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=version,
                name=m.group(2),
                filename=path.name,
                sql=sql,
                checksum=checksum(sql),
            )
        )
    found.sort(key=lambda mig: mig.version)
    return found


def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version    INTEGER     PRIMARY KEY,
            name       TEXT        NOT NULL,
            checksum   TEXT        NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def _applied_rows(conn) -> Dict[int, dict]:
    cur = conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    )
    return {row["version"]: row for row in cur.fetchall()}


# Lets the /health probe report the schema version without a query. Migrations
# only run at startup, so it cannot go stale in-process.
_last_status: Dict[str, object] = {"available": False}


def apply_pending(log: Optional[logging.Logger] = None) -> Dict[str, object]:
    """Apply every migration not yet recorded and return a summary, which is
    also cached for :func:`cached_status`."""
    log = log or logger

    try:
        available = discover()
    except ValueError as e:
        log.error("Migration discovery failed: %s", e)
        _last_status.update({"available": False, "error": str(e)})
        return dict(_last_status)

    if not available:
        # In a container this almost always means a missing COPY line, which
        # would otherwise look identical to "nothing to do".
        log.warning(
            "No migration files found in %s. If this is a deployed container, the "
            "Dockerfile is missing its `COPY migrations ./migrations` line.",
            MIGRATIONS_DIR,
        )

    applied_now: List[Migration] = []
    drift: List[Dict[str, object]] = []

    with get_connection() as conn:
        lock_schema_init(conn)
        _ensure_table(conn)
        already = _applied_rows(conn)

        for mig in available:
            prior = already.get(mig.version)
            if prior is not None:
                if prior["checksum"] != mig.checksum:
                    drift.append(
                        {
                            "version": mig.version,
                            "name": mig.name,
                            "applied_checksum": prior["checksum"],
                            "file_checksum": mig.checksum,
                        }
                    )
                    log.error(
                        "Migration %s was edited AFTER being applied (applied %s, file "
                        "%s). Databases that ran the old text will not match this "
                        "image. Write a NEW migration instead of editing an applied one.",
                        mig.filename, prior["checksum"][:12], mig.checksum[:12],
                    )
                continue

            if _is_effectively_empty(mig.sql):
                log.info("Migration %s is comment-only; recording without executing",
                         mig.filename)
            else:
                log.info("Applying migration %s", mig.filename)
                conn.execute(mig.sql)

            conn.execute(
                "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (mig.version, mig.name, mig.checksum),
            )
            applied_now.append(mig)

    current = max([*already, *(m.version for m in applied_now)], default=None)
    applied_names = [m.filename for m in applied_now]

    if applied_now:
        log.info("Applied %d migration(s): %s", len(applied_now), ", ".join(applied_names))
    else:
        log.info("Schema up to date at version %s (%d migration(s) known)",
                 current, len(available))

    _last_status.clear()
    _last_status.update(
        {
            "available": True,
            "version": current,
            "known": len(available),
            "applied_now": applied_names,
            "drift": drift,
        }
    )
    return dict(_last_status)


def cached_status() -> Dict[str, object]:
    """The startup snapshot, with no DB access, for ``/health``."""
    return dict(_last_status)


def status() -> Dict[str, object]:
    """Live schema state for the admin dashboard. Unlike :func:`cached_status`
    it costs a query, and reports ``pending``: files in this image the database
    has not recorded, the sign of a replica ahead of the schema."""
    try:
        available = discover()
    except ValueError as e:
        return {"available": False, "error": str(e)}

    by_version = {m.version: m for m in available}
    try:
        with get_connection() as conn:
            already = _applied_rows(conn)
    except Exception as e:
        logger.error("Schema status query failed: %s", e)
        return {"available": False, "error": "database unavailable"}

    pending = [m.filename for m in available if m.version not in already]
    drift = [
        {"version": v, "name": row["name"]}
        for v, row in already.items()
        if v in by_version and by_version[v].checksum != row["checksum"]
    ]
    return {
        "available": True,
        "version": max(already, default=None),
        "known": len(available),
        "applied": len(already),
        "pending": pending,
        "drift": drift,
        "history": [
            {
                "version": row["version"],
                "name": row["name"],
                "applied_at": row["applied_at"].isoformat() if row["applied_at"] else None,
            }
            for row in already.values()
        ],
    }


if __name__ == "__main__":  # pragma: no cover
    import json

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(status(), indent=2, default=str))
