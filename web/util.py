"""Small request/format helpers shared across the route modules and the pipeline.

All lifted verbatim from ``api.py`` — grouped here so both the routers and
``web.pipeline`` can share them without importing ``api.py``.
"""

import json
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi.requests import Request

# Sent to the proxy + client so progressive lines actually flush through instead of
# being buffered until the response completes (nginx buffers by default).
_STREAM_HEADERS = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}


def _ndjson(obj: Dict) -> str:
    """Serialize one NDJSON record: a single JSON object followed by a newline."""
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _public_base_url(request: Request) -> str:
    """Public base URL of this backend, honoring reverse-proxy forwarded headers.

    Behind a TLS-terminating reverse proxy (our Docker deploy), uvicorn sees a
    plain HTTP request, so ``request.base_url`` reports ``http://`` — which makes
    the absolute proxy/stream URLs we emit for the operator-owned sources (Jellyfin,
    local, cache) get blocked as mixed content on the HTTPS frontend. Trust
    ``X-Forwarded-Proto``/``X-Forwarded-Host`` (set by the proxy) so the URL is
    HTTPS, regardless of uvicorn's --proxy-headers/--forwarded-allow-ips config.
    """
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        # X-Forwarded-Proto can be a comma-separated list ("https,http").
        proto = proto.split(",")[0].strip()
        return f"{proto}://{host}/"
    return str(request.base_url)


def _is_future_air_date(air_date: Optional[str]) -> bool:
    """True when a TMDB episode air_date is strictly after today (UTC).

    TMDB air dates are bare calendar dates ('YYYY-MM-DD', no time/zone), so an
    episode airing *today* counts as aired — only a strictly-later date is "not
    yet aired". Unknown/empty/garbage dates are treated as aired so we never block
    playback on missing metadata."""
    if not air_date:
        return False
    try:
        d = datetime.strptime(air_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return d > datetime.now(timezone.utc).date()


def _year_from_date(date_str: Optional[str]) -> Optional[int]:
    """Pull the 4-digit year off a TMDB date ("2023-07-21" -> 2023)."""
    if not date_str or len(date_str) < 4 or not date_str[:4].isdigit():
        return None
    return int(date_str[:4])
