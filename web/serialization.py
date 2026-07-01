"""gzip-aware JSON response helpers for the large, non-streaming endpoints.

Applied per-response (not via global middleware) so the progressive NDJSON /watch
stream is never buffered. Lifted verbatim out of ``api.py``.
"""

import gzip
from typing import Dict, Optional, Tuple

import orjson
from fastapi.requests import Request
from fastapi.responses import Response


def _json_gzip_bodies(payload: Dict) -> Tuple[bytes, Optional[bytes]]:
    """Encode ``payload`` to JSON bytes and, when it's worth compressing, its gzip.

    Returns ``(raw_bytes, gzipped_bytes_or_None)``. Split out from ``_gzip_json`` so
    a caller that serves the same payload repeatedly (e.g. the unfiltered
    /catalogue) can cache this once and rebuild the per-request Response cheaply via
    ``_gzip_response`` instead of re-serializing + re-gzipping every time."""
    raw = orjson.dumps(payload)
    gz = gzip.compress(raw, compresslevel=6) if len(raw) >= 1024 else None
    return raw, gz


def _gzip_response(request: Request, bodies: Tuple[bytes, Optional[bytes]]) -> Response:
    """Build the JSON Response from pre-encoded ``bodies``, picking the gzip variant
    when the client accepts it and one was produced."""
    raw, gz = bodies
    headers = {"Vary": "Accept-Encoding"}
    if gz is not None and "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(content=gz, media_type="application/json", headers=headers)
    return Response(content=raw, media_type="application/json", headers=headers)


def _gzip_json(request: Request, payload: Dict) -> Response:
    """Serialize ``payload`` as JSON, gzip-compressing it when the client accepts
    gzip and the body is worth compressing. Used for the large, non-streaming
    endpoints (e.g. /catalogue) — applied per-response instead of via global
    middleware so the progressive NDJSON /watch stream is never buffered."""
    return _gzip_response(request, _json_gzip_bodies(payload))
