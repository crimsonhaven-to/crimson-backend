"""gzip-aware JSON response helpers for the large, non-streaming endpoints.

Applied per response rather than as global middleware, so the progressive NDJSON
/watch stream is never buffered.
"""

import gzip
from typing import Dict, Optional, Tuple

import orjson
from fastapi.requests import Request
from fastapi.responses import Response


def _json_gzip_bodies(payload: Dict) -> Tuple[bytes, Optional[bytes]]:
    """Encode ``payload`` to JSON bytes plus, when worth compressing, its gzip.

    Split out from ``_gzip_json`` so a caller serving the same payload repeatedly
    can cache this once and rebuild each Response via ``_gzip_response`` instead
    of re-serializing and re-gzipping every time."""
    raw = orjson.dumps(payload)
    gz = gzip.compress(raw, compresslevel=6) if len(raw) >= 1024 else None
    return raw, gz


def _gzip_response(request: Request, bodies: Tuple[bytes, Optional[bytes]]) -> Response:
    """Build the Response from pre-encoded ``bodies``, taking the gzip variant when
    the client accepts it and one was produced."""
    raw, gz = bodies
    headers = {"Vary": "Accept-Encoding"}
    if gz is not None and "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(content=gz, media_type="application/json", headers=headers)
    return Response(content=raw, media_type="application/json", headers=headers)


def _gzip_json(request: Request, payload: Dict) -> Response:
    """Serialize ``payload``, gzipping when the client accepts it and the body is
    worth compressing."""
    return _gzip_response(request, _json_gzip_bodies(payload))
