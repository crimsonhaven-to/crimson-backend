"""
Process-wide shared httpx.AsyncClient and the TMDB retry helper.

One warm client, with its keep-alive connections to TMDB and AniList, is the
biggest latency win on the metadata endpoints. api.py's lifespan drives
``open_client`` / ``close_client``.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Dict, Optional

import httpx

from core.config import get_settings

logger = logging.getLogger("crimson.http")

REQUEST_TIMEOUT = 30.0
MAX_RETRIES = 3
RETRY_BACKOFF = 1.0


def tmdb_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {get_settings().tmdb_api_key}",
        "accept": "application/json",
    }


def open_client() -> None:
    """Open the shared client; called from api.py's lifespan startup."""
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    )


async def close_client() -> None:
    """Close the shared client; called from api.py's lifespan shutdown."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


# One AsyncClient for the process rather than a fresh one per request, so the
# TCP+TLS connections stay warm instead of paying a handshake every call. Call
# sites use the ``http_client()`` manager below, which yields this instance and
# deliberately does not close it on exit.
_http_client: Optional[httpx.AsyncClient] = None


def get_http_client() -> httpx.AsyncClient:
    """The shared AsyncClient, with a transient fallback if the lifespan has not
    run yet, which only happens outside the request path."""
    if _http_client is None:
        return httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
    return _http_client


@asynccontextmanager
async def http_client():
    """Yield the shared AsyncClient. A drop-in for ``httpx.AsyncClient()`` at the
    ``async with`` call sites, except that it stays open when the block exits."""
    yield get_http_client()


async def fetch_with_retry(client: httpx.AsyncClient, url: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """GET with backoff on 429 and transient 5xx. None once retries are spent."""
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.get(url, headers=tmdb_headers(), params=params, timeout=REQUEST_TIMEOUT)
            
            if response.status_code == 200:
                return response.json()
            elif response.status_code == 429:
                wait_time = RETRY_BACKOFF * (2 ** attempt)
                logger.warning(f"Rate limited, waiting {wait_time}s before retry {attempt + 1}")
                await asyncio.sleep(wait_time)
                continue
            elif response.status_code in (500, 502, 503, 504):
                # TMDB occasionally 502s on individual records (its status_code 43),
                # so back off rather than treating this as a hard failure.
                logger.warning(
                    f"TMDB upstream {response.status_code} for URL {url} "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})"
                )
                if attempt == MAX_RETRIES - 1:
                    return None
                await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
                continue
            else:
                logger.warning(f"TMDB API error: Status {response.status_code} for URL {url}")
                return None
                
        except httpx.TimeoutException:
            logger.warning(f"Timeout on attempt {attempt + 1} for {url}")
            if attempt == MAX_RETRIES - 1:
                return None
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
        except Exception as e:
            logger.error(f"Request error on attempt {attempt + 1}: {e}")
            if attempt == MAX_RETRIES - 1:
                return None
            await asyncio.sleep(RETRY_BACKOFF * (2 ** attempt))
    
    return None
