"""Request and format helpers shared by the route modules and the pipeline."""

import json
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi.requests import Request

# Makes progressive lines flush through instead of being buffered until the
# response completes, since nginx buffers by default.
_STREAM_HEADERS = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}


def _ndjson(obj: Dict) -> str:
    """Serialize one NDJSON record: a JSON object followed by a newline."""
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _public_base_url(request: Request) -> str:
    """Public base URL of this backend, honoring forwarded headers.

    Behind a TLS-terminating proxy uvicorn sees plain HTTP, so ``base_url`` would
    report ``http://`` and the absolute stream URLs emitted for the
    operator-owned sources would be blocked as mixed content on an HTTPS
    frontend. Trusting the forwarded headers avoids depending on uvicorn's
    --proxy-headers configuration.
    """
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        # X-Forwarded-Proto can be a comma-separated list.
        proto = proto.split(",")[0].strip()
        return f"{proto}://{host}/"
    return str(request.base_url)


def _is_future_air_date(air_date: Optional[str]) -> bool:
    """True when a TMDB air_date is strictly after today, UTC.

    TMDB dates carry no time or zone, so an episode airing today counts as aired.
    An unknown or malformed date also counts as aired, so missing metadata never
    blocks playback."""
    if not air_date:
        return False
    try:
        d = datetime.strptime(air_date[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return d > datetime.now(timezone.utc).date()


def _year_from_date(date_str: Optional[str]) -> Optional[int]:
    """Pull the 4-digit year off a TMDB date."""
    if not date_str or len(date_str) < 4 or not date_str[:4].isdigit():
        return None
    return int(date_str[:4])
