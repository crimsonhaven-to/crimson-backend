"""
Process-wide shared httpx.AsyncClient + the TMDB retry helper, lifted out of api.py.

Keeping one warm client (and its TMDB/AniList keep-alive connections) is the
biggest latency win on the metadata endpoints. The fetchers import ``http_client``
/ ``fetch_with_retry`` from here; api.py's lifespan drives ``open_client`` /
``close_client``.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict, Optional

import httpx

from core.config import Config, TMDB_HEADERS

logger = logging.getLogger("crimson.http")


def open_client() -> None:
    """Open the shared client (called from api.py's lifespan startup)."""
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=Config.REQUEST_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    )


async def close_client() -> None:
    """Close the shared client (called from api.py's lifespan shutdown)."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


# --- SHARED HTTP CLIENT ---
# One process-wide AsyncClient (opened in lifespan) instead of a fresh
# httpx.AsyncClient() per request. Reusing it keeps the TCP+TLS connections to
# TMDB / AniList warm across requests rather than paying a new handshake every
# call — the single biggest latency win on the metadata endpoints. Call sites use
# the ``http_client()`` context manager below, which yields this shared instance
# and deliberately does NOT close it on block exit.
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating a transient fallback if the
    lifespan hasn't run yet (only possible outside the normal request path)."""
    if _http_client is None:
        return httpx.AsyncClient(timeout=Config.REQUEST_TIMEOUT)
    return _http_client


@asynccontextmanager
async def http_client():
    """Yield the shared AsyncClient. Drop-in for ``httpx.AsyncClient()`` at the
    existing ``async with ... as client:`` call sites — but the shared client is
    kept open (not closed) when the block exits."""
    yield get_http_client()


# --- TMDB API FUNCTIONS ---
async def fetch_with_retry(client: httpx.AsyncClient, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """Fetch data from API with retry logic"""
    for attempt in range(Config.MAX_RETRIES):
        try:
            response = await client.get(url, headers=TMDB_HEADERS, params=params, timeout=Config.REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:  # Rate limit
                wait_time = Config.RETRY_BACKOFF_FACTOR * (2 ** attempt)
                logger.warning(f"Rate limited, waiting {wait_time}s before retry {attempt + 1}")
                await asyncio.sleep(wait_time)
                continue
            elif response.status_code in (500, 502, 503, 504):
                # Transient upstream failure (TMDB occasionally 502s on individual
                # records — see status_code 43 "Couldn't connect to the backend").
                # Back off and retry rather than treating it as a hard failure.
                logger.warning(
                    f"TMDB upstream {response.status_code} for URL {url} "
                    f"(attempt {attempt + 1}/{Config.MAX_RETRIES})"
                )
                if attempt == Config.MAX_RETRIES - 1:
                    return None
                await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
                continue
            else:
                logger.warning(f"TMDB API error: Status {response.status_code} for URL {url}")
                return None
                
        except httpx.TimeoutException:
            logger.warning(f"Timeout on attempt {attempt + 1} for {url}")
            if attempt == Config.MAX_RETRIES - 1:
                return None
            await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
        except Exception as e:
            logger.error(f"Request error on attempt {attempt + 1}: {e}")
            if attempt == Config.MAX_RETRIES - 1:
                return None
            await asyncio.sleep(Config.RETRY_BACKOFF_FACTOR * (2 ** attempt))
    
    return None
