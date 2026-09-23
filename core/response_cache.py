"""
Two-tier response cache.

L1 is an in-process TTL dict in front of the L2 PostgreSQL ``api_cache`` table,
saving a DB round-trip on the hottest fixed-key payloads. Lives here rather than
in api.py, which would be a circular import for the metadata fetchers.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import orjson

from core.db_pool import get_connection
from core import observability

logger = logging.getLogger("crimson.cache")

CACHE_TTL = 24 * 3600
TRENDING_CACHE_TTL = 6 * 3600


def _utcnow_iso() -> str:
    """Current UTC time as a naive ISO-8601 string.

    ``datetime.utcnow()`` is deprecated, so UTC is derived from a tz-aware ``now``
    with the offset dropped. That keeps the exact shape existing api_cache rows
    were written with, so lexicographic ``expires_at`` comparisons stay correct.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


# L1, for the few hot keys with a fixed global cache key. Each replica converges
# independently: no cross-replica invalidation is needed because these payloads
# are read-mostly and already TTL-bounded upstream.
_LOCAL_CACHE_TTL = 300  # seconds


_local_cache: Dict[str, Tuple[float, object]] = {}


def _local_get(key: str):
    # Counted without the key as a label: keys carry per-search and per-title
    # values, so that would be unbounded cardinality. The per-tier ratio is the
    # useful number anyway.
    hit = _local_cache.get(key)
    if not hit:
        observability.record_cache_lookup("l1", False)
        return None
    expiry, value = hit
    if expiry < time.monotonic():
        _local_cache.pop(key, None)
        observability.record_cache_lookup("l1", False)
        return None
    observability.record_cache_lookup("l1", True)
    return value


def _local_set(key: str, value: object, ttl: int = _LOCAL_CACHE_TTL) -> None:
    _local_cache[key] = (time.monotonic() + ttl, value)


async def get_cached_response(cache_key: str) -> Optional[Dict]:
    """The L2 (database) entry for ``cache_key``, or None when absent or expired."""
    try:
        def _query():
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT response_json FROM api_cache WHERE cache_key = %s AND expires_at > %s",
                    (cache_key, _utcnow_iso())
                )
                row = cursor.fetchone()
                return orjson.loads(row["response_json"]) if row else None
        
        loop = asyncio.get_event_loop()
        row = await loop.run_in_executor(None, _query)
        observability.record_cache_lookup("l2", row is not None)
        return row
    except Exception as e:
        logger.error(f"Cache retrieval error for key {cache_key}: {e}")
        return None


async def set_cached_response(cache_key: str, data: Dict, ttl_seconds: int = CACHE_TTL):
    """Upsert an L2 entry. A no-op on empty data."""
    if not data:
        return
    
    try:
        expires_at = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ttl_seconds)).isoformat()
        # response_json is TEXT and orjson.dumps returns bytes.
        payload = orjson.dumps(data).decode("utf-8")
        
        def _insert():
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO api_cache (cache_key, response_json, expires_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (cache_key) DO UPDATE SET
                        response_json=EXCLUDED.response_json, expires_at=EXCLUDED.expires_at
                """, (cache_key, payload, expires_at))
        
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _insert)
    except Exception as e:
        logger.error(f"Cache storage error for key {cache_key}: {e}")


# --- SERVE-STALE-ON-ERROR ---------------------------------------------------
# A last-known-good shadow beside the normal short-TTL entry, so a failed upstream
# leaves the discovery hubs serving the previous result instead of an empty grid.
# It only surfaces during an outage, so it can safely be far older than any fresh
# TTL; a week outlives a prolonged outage while still expiring dead content.
STALE_TTL_SECONDS = 7 * 24 * 3600


def _stale_key(cache_key: str) -> str:
    return f"stale:{cache_key}"


async def set_cached_response_shadowed(cache_key: str, data: Dict, ttl_seconds: int = CACHE_TTL):
    """Write the fresh entry and its long-lived shadow.

    A no-op on empty data, like ``set_cached_response``, so a failed fetch never
    overwrites a good shadow with nothing.
    """
    if not data:
        return
    await set_cached_response(cache_key, data, ttl_seconds)
    await set_cached_response(_stale_key(cache_key), data, STALE_TTL_SECONDS)


async def get_stale_response(cache_key: str) -> Optional[Dict]:
    """The last known good copy of ``cache_key``, if still retained. Read only on
    the failure path; a success always prefers ``get_cached_response``."""
    return await get_cached_response(_stale_key(cache_key))


def purge_expired_cache() -> int:
    """Delete expired api_cache rows. Returns the number removed."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM api_cache WHERE expires_at < %s", (_utcnow_iso(),))
        return cursor.rowcount or 0
