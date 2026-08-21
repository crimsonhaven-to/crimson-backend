"""
Shared PostgreSQL connection pool (psycopg 3).

The mapping engine and the account engine share one database through one
process-wide pool, so every replica in a Swarm deploy points at the same external
database and the containers stay identical. That is what SQLite on a volume could
not offer.

Both concerns share a database because a Fribb resync only DELETEs the three
mapping tables inside one transaction and never touches the account tables. The
historical reason to keep them in separate files no longer applies.

Configuration, read lazily on first use so ``load_dotenv()`` has run:

``DATABASE_URL``    full libpq URL, taking precedence when set
otherwise assembled from ``POSTGRES_HOST`` (localhost), ``POSTGRES_PORT`` (5432),
``POSTGRES_DB`` / ``POSTGRES_USER`` / ``POSTGRES_PASSWORD`` (all crimson).
Sizing is ``DB_POOL_MIN`` (1) and ``DB_POOL_MAX`` (10); startup waits
``DB_CONNECT_TIMEOUT`` seconds (30) for the DB to accept connections.

``DB_PREPARE_THRESHOLD`` defaults to disabled. psycopg auto-prepares a statement
after a few uses, but prepared statements do not survive PgBouncer's transaction
pooling, where a later EXECUTE can land on a different backend than the PREPARE.
Since the documented topology puts the app behind a transaction-mode PgBouncer,
off is correct either way and these queries are simple enough that the lost plan
caching is negligible. Set an integer to re-enable it for a direct connection.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Created on first use, double-checked so a burst of concurrent first callers
# from the thread pool only builds one.
_pool: Optional[ConnectionPool] = None
_lock = threading.Lock()

# Shared by every init_db() that creates schema. `CREATE TABLE IF NOT EXISTS` is
# not safe under catalog contention: replicas booting together race and one dies
# with "tuple concurrently updated". Taking this lock first serializes them, and
# the loser then runs the DDL as a harmless no-op. Transaction-scoped, so it
# releases when init_db commits.
SCHEMA_INIT_LOCK = 0x6372736E  # "crsn"


def lock_schema_init(conn) -> None:
    """Take the schema-init advisory lock on ``conn``'s transaction. Call it first
    inside an init_db() block so concurrent replica startups don't race on DDL."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_INIT_LOCK,))


def _dsn() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    db = os.getenv("POSTGRES_DB", "crimson")
    user = os.getenv("POSTGRES_USER", "crimson")
    password = os.getenv("POSTGRES_PASSWORD", "crimson")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def _prepare_threshold() -> Optional[int]:
    """psycopg ``prepare_threshold`` for pooled connections; see the module
    docstring for why it defaults to disabled."""
    raw = os.getenv("DB_PREPARE_THRESHOLD")
    if raw is None or raw.strip().lower() in ("", "none", "disabled", "off"):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_pool() -> ConnectionPool:
    """The shared pool, opened on first call.

    ``dict_row`` is set pool-wide so every borrowed connection yields dict rows.
    """
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                pool = ConnectionPool(
                    conninfo=_dsn(),
                    min_size=int(os.getenv("DB_POOL_MIN", "1")),
                    max_size=int(os.getenv("DB_POOL_MAX", "10")),
                    kwargs={
                        "row_factory": dict_row,
                        # Off by default so transaction-mode PgBouncer is safe.
                        "prepare_threshold": _prepare_threshold(),
                    },
                    name="crimson",
                    open=False,
                )
                pool.open()
                # Block briefly so a cold start reports an unreachable DB here
                # rather than as a confusing first-request 500.
                pool.wait(timeout=float(os.getenv("DB_CONNECT_TIMEOUT", "30")))
                _pool = pool
    return _pool


def get_connection():
    """Borrow a pooled connection as a context manager.

    A clean exit commits the transaction and an exception rolls it back; either
    way the connection returns to the pool.
    """
    return get_pool().connection()


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """:func:`get_connection` as a generator context manager, where the explicit
    ``with connection() as conn`` reads better."""
    with get_pool().connection() as conn:
        yield conn


def pool_stats() -> dict:
    """Live pool utilisation and configured bounds, for the admin dashboard.

    Merges psycopg_pool's own counters with the configured min/max so the
    dashboard can show headroom. Reports ``available: False`` if the pool is not
    open yet."""
    if _pool is None:
        return {"available": False}
    try:
        raw = _pool.get_stats()
    except Exception:
        raw = {}
    size = raw.get("pool_size", 0)
    in_use = raw.get("pool_size") - raw.get("pool_available", 0) if raw.get("pool_size") is not None else None
    return {
        "available": True,
        "min_size": getattr(_pool, "min_size", None),
        "max_size": getattr(_pool, "max_size", None),
        "size": size,
        "idle": raw.get("pool_available"),
        "in_use": in_use,
        "waiting": raw.get("requests_waiting"),
        "requests_total": raw.get("requests_num"),
        "requests_errors": raw.get("requests_errors"),
        "connections_total": raw.get("connections_num"),
    }


def close_pool() -> None:
    """Close the pool; called on application shutdown."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
