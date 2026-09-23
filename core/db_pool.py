"""The process-wide PostgreSQL connection pool (psycopg 3).

Connection settings come from ``DATABASE_URL``, or else the ``POSTGRES_*``
parts. ``DB_PREPARE_THRESHOLD`` stays disabled by default: prepared statements do
not survive PgBouncer's transaction pooling, where a later EXECUTE can land on a
different backend than the PREPARE, and these queries gain little from plan
caching. Set an integer to re-enable it on a direct connection.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from core.config import Settings, get_settings

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


def _dsn(s: Settings) -> str:
    if s.database_url:
        return s.database_url
    return (
        f"postgresql://{s.postgres_user}:{s.postgres_password}"
        f"@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}"
    )


def get_pool() -> ConnectionPool:
    """The shared pool, opened on first call.

    ``dict_row`` is set pool-wide so every borrowed connection yields dict rows.
    """
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                settings = get_settings()
                pool = ConnectionPool(
                    conninfo=_dsn(settings),
                    min_size=settings.db_pool_min,
                    max_size=settings.db_pool_max,
                    kwargs={
                        "row_factory": dict_row,
                        "prepare_threshold": settings.db_prepare_threshold,
                    },
                    name="crimson",
                    open=False,
                )
                pool.open()
                # Block briefly so a cold start reports an unreachable DB here
                # rather than as a confusing first-request 500.
                pool.wait(timeout=settings.db_connect_timeout)
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
