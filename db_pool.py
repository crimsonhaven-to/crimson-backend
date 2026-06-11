"""
Shared PostgreSQL connection pool (psycopg 3).

This replaces the per-file SQLite connections the backend used to open. Both the
metadata mapping engine and the account engine now talk to a single PostgreSQL
database through one process-wide pool, so a multi-replica / Docker Swarm deploy
can point every container at the same external database — no shared-volume,
single-writer gymnastics, every container identical (the Infrastructure-as-Code
goal that SQLite-on-a-volume could not satisfy).

One database for both concerns
------------------------------
The mapping tables (``anime_entries`` / ``tmdb_*`` / ``api_cache`` / ``sync_meta``)
used to live in their own SQLite file, deliberately separate from the accounts
file, because a Fribb resync wipes the mapping wholesale and would have taken
user data with it. Under PostgreSQL the resync only DELETEs the three mapping
tables inside one transaction — it never touches the account tables — so the
historical reason to keep them physically apart is gone and a single pooled
database is simpler and cheaper to pool.

Configuration (read lazily on first use, i.e. after ``load_dotenv()``)
----------------------------------------------------------------------
``DATABASE_URL``   full libpq URL; takes precedence when set, e.g.
                   ``postgresql://crimson:crimson@localhost:5432/crimson``
otherwise assembled from the discrete parts:
``POSTGRES_HOST`` (localhost), ``POSTGRES_PORT`` (5432), ``POSTGRES_DB`` (crimson),
``POSTGRES_USER`` (crimson), ``POSTGRES_PASSWORD`` (crimson).
Pool sizing: ``DB_POOL_MIN`` (1), ``DB_POOL_MAX`` (10). Startup wait for the DB
to accept connections: ``DB_CONNECT_TIMEOUT`` seconds (30).

``DB_PREPARE_THRESHOLD`` controls psycopg's server-side prepared statements.
psycopg auto-prepares a statement after it's been used a few times
(``prepare_threshold``, default 5). Those prepared statements do NOT survive
PgBouncer's *transaction* pooling — a later EXECUTE can land on a different
Postgres backend than the one that ran PREPARE — so the documented production
topology (the app behind a transaction-mode PgBouncer; see deploy/pgbouncer)
needs them OFF. We therefore default ``prepare_threshold`` to ``None`` (disabled);
the queries here are short and simple, so the lost plan-caching is negligible,
and this is correct whether or not a pooler is in front. Set
``DB_PREPARE_THRESHOLD`` to an integer (e.g. ``5``) to re-enable it when
connecting straight to Postgres with no pooler.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Process-wide singleton pool, created on first use (double-checked locking so a
# burst of concurrent first-callers from the FastAPI thread pool only build one).
_pool: Optional[ConnectionPool] = None
_lock = threading.Lock()

# Advisory-lock key shared by every init_db() that creates schema. `CREATE TABLE
# / INDEX IF NOT EXISTS` is NOT safe under catalog contention — several replicas
# booting at once race and one crashes with "tuple concurrently updated" /
# "duplicate key ... pg_type_typname_nsp_index". Each init_db takes
# pg_advisory_xact_lock(SCHEMA_INIT_LOCK) as its first statement so simultaneous
# boots serialize (the loser waits, then runs the DDL as a harmless no-op). It's
# transaction-scoped, so it auto-releases when the init_db transaction commits.
SCHEMA_INIT_LOCK = 0x6372736E  # "crsn"


def lock_schema_init(conn) -> None:
    """Take the cluster-wide schema-init advisory lock on ``conn``'s current
    transaction. Call this first inside an init_db() ``with get_connection()``
    block so concurrent replica startups don't race on DDL."""
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
    """psycopg ``prepare_threshold`` for pooled connections (see module docstring).

    Defaults to ``None`` (auto-prepare disabled) so the app is safe behind a
    transaction-mode PgBouncer. ``DB_PREPARE_THRESHOLD`` may set an integer to
    re-enable it for a direct (pooler-less) Postgres connection.
    """
    raw = os.getenv("DB_PREPARE_THRESHOLD")
    if raw is None or raw.strip().lower() in ("", "none", "disabled", "off"):
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def get_pool() -> ConnectionPool:
    """Return the shared pool, opening it on first call.

    ``dict_row`` is set pool-wide so every borrowed connection yields dict rows
    (``row["col"]``) — the closest drop-in for the old ``sqlite3.Row`` access.
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
                        # OFF by default so transaction-mode PgBouncer is safe;
                        # see _prepare_threshold / the module docstring.
                        "prepare_threshold": _prepare_threshold(),
                    },
                    name="crimson",
                    open=False,
                )
                pool.open()
                # Block briefly so a cold start surfaces an unreachable DB as a
                # clear error here rather than as a confusing first-request 500.
                pool.wait(timeout=float(os.getenv("DB_CONNECT_TIMEOUT", "30")))
                _pool = pool
    return _pool


def get_connection():
    """Borrow a pooled connection as a context manager.

    Usage mirrors the old ``sqlite3`` style::

        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(...)

    On a clean exit the transaction is committed; on an exception it is rolled
    back; either way the connection returns to the pool (psycopg_pool semantics).
    """
    return get_pool().connection()


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Same as :func:`get_connection` but as a generator context manager, handy
    where an explicit ``with connection() as conn`` reads better."""
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    """Close the pool (called on application shutdown)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None
