"""Process-wide shared httpx.AsyncClient and the TMDB retry helper.

One warm client, with its keep-alive connections to TMDB and AniList, is the
biggest latency win on the metadata endpoints. The app lifespan drives
``open_client`` / ``close_client``.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional

import httpx

from core.config import get_settings

logger = logging.getLogger("crimson.http")

REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0

# TMDB occasionally 502s on individual records (its status_code 43), so transient
# 5xx are retried like a 429 rather than treated as a hard failure.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

_http_client: Optional[httpx.AsyncClient] = None


def tmdb_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {get_settings().tmdb_api_key}",
        "accept": "application/json",
    }


def open_client() -> None:
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    )


async def close_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


@asynccontextmanager
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Yield the shared client and leave it open on exit. Before the lifespan has
    opened it (scripts, tests), a temporary client is opened and closed instead
    so nothing leaks."""
    if _http_client is not None:
        yield _http_client
        return
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        yield client


async def fetch_with_retry(client: httpx.AsyncClient, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """GET with backoff on 429 and transient 5xx. None once retries are spent."""
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.get(url, headers=tmdb_headers(), params=params, timeout=REQUEST_TIMEOUT)
        except httpx.TimeoutException:
            logger.warning("Timeout on attempt %d/%d for %s", attempt + 1, MAX_RETRIES, url)
        except Exception as e:
            logger.error("Request error on attempt %d/%d for %s: %s", attempt + 1, MAX_RETRIES, url, e)
        else:
            if response.status_code == 200:
                return response.json()
            if response.status_code not in _RETRYABLE_STATUS:
                logger.warning("TMDB API error: status %d for %s", response.status_code, url)
                return None
            logger.warning(
                "TMDB upstream %d for %s (attempt %d/%d)",
                response.status_code, url, attempt + 1, MAX_RETRIES,
            )
        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
    return None
