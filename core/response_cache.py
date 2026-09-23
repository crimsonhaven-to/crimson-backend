"""Two-tier response cache: an in-process L1 dict in front of the L2 ``api_cache``
table.

Each replica keeps its own L1 and converges on its own: the payloads are
read-mostly and TTL-bounded, so no cross-replica invalidation is needed.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import orjson

from core import metrics
from core.bounded_cache import BoundedCache
from core.db_pool import get_connection

logger = logging.getLogger("crimson.cache")

CACHE_TTL = 24 * 3600
TRENDING_CACHE_TTL = 6 * 3600
_LOCAL_TTL = 300
# Keys include per-title and per-search values, so the dict needs a ceiling.
_LOCAL_MAX = 5000

_local = BoundedCache(_LOCAL_MAX)


def _utcnow_iso() -> str:
    """Naive UTC, the exact shape existing api_cache rows were written with, so
    the lexicographic ``expires_at`` comparisons stay correct."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def local_get(key: str):
    # Counted without the key as a label, which would be unbounded cardinality.
    value = _local.get(key)
    metrics.record_cache_lookup("l1", value is not None)
    return value


def local_set(key: str, value: object, ttl: int = _LOCAL_TTL) -> None:
    _local.set(key, value, ttl)


def local_pop(key: str) -> None:
    _local.pop(key)


def _read(cache_key: str) -> Optional[Dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT response_json FROM api_cache WHERE cache_key = %s AND expires_at > %s",
            (cache_key, _utcnow_iso()),
        ).fetchone()
        return orjson.loads(row["response_json"]) if row else None


def _write(cache_key: str, payload: str, expires_at: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO api_cache (cache_key, response_json, expires_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (cache_key) DO UPDATE SET
                response_json = EXCLUDED.response_json, expires_at = EXCLUDED.expires_at
            """,
            (cache_key, payload, expires_at),
        )


async def get_cached_response(cache_key: str) -> Optional[Dict]:
    """The L2 entry, or None when absent, expired or unreadable."""
    try:
        row = await asyncio.to_thread(_read, cache_key)
    except Exception as e:
        logger.error(f"Cache retrieval error for key {cache_key}: {e}")
        return None
    metrics.record_cache_lookup("l2", row is not None)
    return row


async def set_cached_response(cache_key: str, data: object, ttl_seconds: int = CACHE_TTL) -> None:
    """Upsert an L2 entry. A no-op on empty data."""
    if not data:
        return
    expires_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ttl_seconds)
    ).isoformat()
    try:
        await asyncio.to_thread(_write, cache_key, orjson.dumps(data).decode("utf-8"), expires_at)
    except Exception as e:
        logger.error(f"Cache storage error for key {cache_key}: {e}")


# A last-known-good shadow beside the short-TTL entry, so a failed upstream serves
# the previous result instead of an empty grid. It only surfaces during an outage,
# so a week outlives a long one while still expiring dead content.
STALE_TTL_SECONDS = 7 * 24 * 3600


def _stale_key(cache_key: str) -> str:
    return f"stale:{cache_key}"


async def set_cached_response_shadowed(
    cache_key: str, data: object, ttl_seconds: int = CACHE_TTL
) -> None:
    """Write the fresh entry and its shadow. A no-op on empty data, so a failed
    fetch never overwrites a good shadow with nothing."""
    if not data:
        return
    await set_cached_response(cache_key, data, ttl_seconds)
    await set_cached_response(_stale_key(cache_key), data, STALE_TTL_SECONDS)


async def get_stale_response(cache_key: str) -> Optional[Dict]:
    return await get_cached_response(_stale_key(cache_key))


def purge_expired_cache() -> int:
    """Consume-on-read never deletes rows, and every unique search writes one."""
    with get_connection() as conn:
        return (
            conn.execute("DELETE FROM api_cache WHERE expires_at < %s", (_utcnow_iso(),)).rowcount
            or 0
        )
