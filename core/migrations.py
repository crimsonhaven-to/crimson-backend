"""
Versioned schema migrations.

Why this exists
---------------
Before this module, the schema was defined by nine hand-rolled ``init_db()``
functions holding ~53 DDL statements between them, and the accumulated
``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` lines had become the schema's actual
version history. That works right up until it doesn't: nothing could answer "does
this database match this image?", there was no down path, no way to add a
``NOT NULL`` or backfill data, and with Patroni HA plus rolling Swarm deploys a
version-skewed replica stayed invisible until it threw at request time.

What this module does NOT do
---------------------------
It deliberately does **not** take ownership of the existing schema. The
``init_db()`` functions still run first at startup and are still idempotent, and
their DDL was not transcribed into ``migrations/``. Transcribing it would have
meant a freshly created database and a long-lived production database taking
different code paths to (hopefully) the same schema, which is precisely the class
of divergence this system is meant to prevent. So the baseline stays where it is
and this runner owns everything from version 0 onward.

How it runs
-----------
``apply_pending()`` is called once per process at startup, AFTER every
``init_db()``. It takes the same cluster-wide ``SCHEMA_INIT_LOCK`` advisory lock
those functions take, so simultaneous replica boots serialize: the first replica
applies, the rest wait and then find nothing pending.

Everything happens in ONE transaction: the lock is transaction-scoped
(``pg_advisory_xact_lock``), so holding it for the whole run is what makes
"check what's applied, then apply the rest" atomic against another booting
replica. A failing migration rolls the whole batch back and releases the lock,
leaving the database exactly as it was. The practical consequence is that
statements which cannot run inside a transaction block (notably
``CREATE INDEX CONCURRENTLY``) do not belong in a migration file.

Drift detection
---------------
Each applied file's SHA-256 is stored. On every boot the runner re-hashes the
files on disk and compares. A mismatch means an already-applied migration was
edited after the fact, so some databases ran the old text and some would run the
new one. That is reported loudly and surfaced on ``/health``, but it is
deliberately **not** fatal: refusing to boot on a whitespace change would turn a
bookkeeping problem into an outage.

Ordering note
-------------
Files are ordered by their numeric prefix, not lexicographically, so ``010_`` sorts
after ``009_`` and a hypothetical ``0100_`` after ``099_``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional

from core.db_pool import get_connection, lock_schema_init

logger = logging.getLogger("crimson.migrations")

# Where the .sql files live. Overridable so tests can point at a temp directory
# without touching the real set.
DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# NNN_name.sql -- at least three digits so the directory sorts sanely in a shell
# listing too, and a restricted name charset so nothing odd reaches a log line.
_FILENAME_RE = re.compile(r"^(\d{3,})_([A-Za-z0-9][A-Za-z0-9._-]*)\.sql$")


def migrations_dir() -> Path:
    """The migrations directory, overridable via ``MIGRATIONS_DIR``."""
    override = os.getenv("MIGRATIONS_DIR")
    return Path(override) if override else DEFAULT_MIGRATIONS_DIR


class Migration(NamedTuple):
    version: int
    name: str
    filename: str
    sql: str
    checksum: str


def checksum(sql: str) -> str:
    """SHA-256 of a migration's text, newline-normalised.

    Normalising CRLF to LF matters: this repo is developed on Windows and built in
    a Linux container, and a checkout with different line endings must not read as
    drift."""
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _is_effectively_empty(sql: str) -> bool:
    """True when a file holds only comments/whitespace. Postgres rejects an empty
    query string, so such a file is skipped rather than executed."""
    body = re.sub(r"--[^\n]*", "", sql)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return not body.strip()


def discover(directory: Optional[Path] = None) -> List[Migration]:
    """Every migration file on disk, ordered by numeric version.

    Raises ``ValueError`` on a duplicate version number: two files claiming the
    same version would apply in an arbitrary order, which is never intended."""
    directory = directory or migrations_dir()
    if not directory.is_dir():
        return []

    found: List[Migration] = []
    seen: Dict[int, str] = {}
    for path in sorted(directory.iterdir()):
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


# Snapshot of the last apply_pending() result, so /health can report the schema
# version without adding a DB round-trip to a probe that fires every 30s per
# replica. Migrations only run at startup, so this cannot go stale in-process.
_last_status: Dict[str, object] = {"available": False}


def apply_pending(log: Optional[logging.Logger] = None) -> Dict[str, object]:
    """Apply every migration not yet recorded, in version order.

    Returns a summary dict (also cached for :func:`cached_status`). Safe to call on
    every replica: the advisory lock serializes concurrent boots and an
    already-applied migration is simply skipped."""
    log = log or logger

    try:
        available = discover()
    except ValueError as e:
        log.error("Migration discovery failed: %s", e)
        _last_status.update({"available": False, "error": str(e)})
        return dict(_last_status)

    if not available:
        # An empty directory in a *container* almost always means the COPY line is
        # missing, which would otherwise look identical to "nothing to do".
        log.warning(
            "No migration files found in %s. If this is a deployed container, the "
            "Dockerfile is missing its `COPY migrations ./migrations` line.",
            migrations_dir(),
        )

    applied_now: List[str] = []
    drift: List[Dict[str, object]] = []
    applied_versions: set = set()

    with get_connection() as conn:
        # Same lock the init_db()s take, for the same reason: DDL is not safe under
        # catalog contention when several replicas boot at once.
        lock_schema_init(conn)
        _ensure_table(conn)
        already = _applied_rows(conn)
        applied_versions.update(already.keys())

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
            applied_now.append(mig.filename)
            applied_versions.add(mig.version)

    current = max(applied_versions, default=None)

    if applied_now:
        log.info("Applied %d migration(s): %s", len(applied_now), ", ".join(applied_now))
    else:
        log.info("Schema up to date at version %s (%d migration(s) known)",
                 current, len(available))

    _last_status.clear()
    _last_status.update(
        {
            "available": True,
            "version": current,
            "known": len(available),
            "applied_now": applied_now,
            "drift": drift,
        }
    )
    return dict(_last_status)


def cached_status() -> Dict[str, object]:
    """The startup snapshot, with no DB access. Used by ``/health``."""
    return dict(_last_status)


def status() -> Dict[str, object]:
    """Live schema state, read from the database. Used by the admin dashboard.

    Unlike :func:`cached_status` this costs a query, and it reports ``pending``
    (files present in the image but not recorded in this database) which is the
    signal that a replica is running ahead of or behind the schema."""
    try:
        available = discover()
    except ValueError as e:
        return {"available": False, "error": str(e)}

    by_version = {m.version: m for m in available}
    try:
        with get_connection() as conn:
            _ensure_table(conn)
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


if __name__ == "__main__":  # pragma: no cover - operator convenience
    # `python -m core.migrations` prints the live status without applying anything.
    import json

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(status(), indent=2, default=str))
