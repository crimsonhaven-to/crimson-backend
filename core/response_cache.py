"""
Two-tier response cache, lifted out of api.py.

L1 is a tiny in-process TTL dict in front of the L2 PostgreSQL ``api_cache`` table,
removing a DB round-trip on the hottest fixed-key payloads (trending, catalogue).
Both api.py and the metadata fetchers import these helpers, so they live here
rather than in api.py (which would be a circular import for the fetchers).
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import orjson

from core.config import Config
from core.db_pool import get_connection

logger = logging.getLogger("crimson.cache")


def _utcnow_iso() -> str:
    """Current UTC time as a naive ISO-8601 string.

    ``datetime.utcnow()`` is deprecated (and slated for removal), so we derive
    UTC from a tz-aware ``now`` but drop the offset to keep the exact same
    ``YYYY-MM-DDTHH:MM:SS.ffffff`` shape the api_cache rows were written with —
    so lexicographic ``expires_at`` comparisons stay correct across an upgrade.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


# --- IN-PROCESS L1 CACHE (hot global keys) ---
# A tiny TTL cache in front of the PostgreSQL api_cache for the few hot keys that
# use a fixed global cache key (trending, catalogue). It removes a DB round-trip
# on every hit. Stateless-friendly: it only ever serves data up to its short TTL
# and each replica converges independently (no cross-replica invalidation needed
# because these payloads are read-mostly and already TTL-bounded upstream).
_LOCAL_CACHE_TTL = 300  # seconds


_local_cache: Dict[str, Tuple[float, object]] = {}


def _local_get(key: str):
    hit = _local_cache.get(key)
    if not hit:
        return None
    expiry, value = hit
    if expiry < time.monotonic():
        _local_cache.pop(key, None)
        return None
    return value


def _local_set(key: str, value: object, ttl: int = _LOCAL_CACHE_TTL) -> None:
    _local_cache[key] = (time.monotonic() + ttl, value)


# --- CACHE HELPER FUNCTIONS ---
async def get_cached_response(cache_key: str) -> Optional[Dict]:
    """Retrieve cached response from database"""
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
        return await loop.run_in_executor(None, _query)
    except Exception as e:
        logger.error(f"Cache retrieval error for key {cache_key}: {e}")
        return None


async def set_cached_response(cache_key: str, data: Dict, ttl_seconds: int = Config.CACHE_TTL_SECONDS):
    """Save response to cache"""
    if not data:
        return
    
    try:
        expires_at = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=ttl_seconds)).isoformat()
        # orjson.dumps returns bytes; the response_json column is TEXT, so decode.
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


def purge_expired_cache() -> int:
    """Delete expired api_cache rows. Returns the number removed."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM api_cache WHERE expires_at < %s", (_utcnow_iso(),))
        return cursor.rowcount or 0
