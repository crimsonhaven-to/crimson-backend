"""The process-wide PostgreSQL connection pool (psycopg 3).

``DB_PREPARE_THRESHOLD`` is disabled by default: under PgBouncer's transaction
pooling an EXECUTE can land on a different backend than its PREPARE, and these
queries gain little from plan caching.
"""

from __future__ import annotations

import threading
from typing import Optional

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from core.config import Settings, get_settings

# Created on first use, double-checked so a burst of concurrent first callers
# from the thread pool only builds one.
_pool: Optional[ConnectionPool] = None
_lock = threading.Lock()

# `CREATE TABLE IF NOT EXISTS` is not safe under catalog contention: replicas
# booting together race and one dies with "tuple concurrently updated". Every
# init_db() takes this transaction-scoped lock first, so the loser's DDL is a
# no-op.
SCHEMA_INIT_LOCK = 0x6372736E  # "crsn"


def lock_schema_init(conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_INIT_LOCK,))


def _dsn(s: Settings) -> str:
    if s.database_url:
        return s.database_url
    return (
        f"postgresql://{s.postgres_user}:{s.postgres_password}"
        f"@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}"
    )


def get_pool() -> ConnectionPool:
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
    """Borrow a pooled connection as a context manager: a clean exit commits,
    an exception rolls back."""
    return get_pool().connection()


def pool_stats() -> dict:
    """Pool counters plus the configured bounds, for the admin dashboard."""
    if _pool is None:
        return {"available": False}
    try:
        raw = _pool.get_stats()
    except Exception:
        raw = {}
    # psycopg_pool reports both counters or, if get_stats failed, neither.
    idle = raw.get("pool_available")
    return {
        "available": True,
        "min_size": _pool.min_size,
        "max_size": _pool.max_size,
        "size": raw.get("pool_size", 0),
        "idle": idle,
        "in_use": raw["pool_size"] - idle if idle is not None else None,
        "waiting": raw.get("requests_waiting"),
        "requests_total": raw.get("requests_num"),
        "requests_errors": raw.get("requests_errors"),
        "connections_total": raw.get("connections_num"),
    }


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
