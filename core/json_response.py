"""gzip-aware JSON for the large, non-streaming endpoints.

Per response rather than as middleware, so the progressive NDJSON streams are
never buffered.
"""

import gzip
from typing import Dict, Optional, Tuple

import orjson
from fastapi.requests import Request
from fastapi.responses import Response

# Below this, gzip costs more than it saves.
_GZIP_MIN_BYTES = 1024

Bodies = Tuple[bytes, Optional[bytes]]


def encode(payload: Dict) -> Bodies:
    """The JSON bytes and, when worth it, their gzip. A caller serving the same
    payload repeatedly caches this and rebuilds each response with ``respond``."""
    raw = orjson.dumps(payload)
    return raw, gzip.compress(raw, compresslevel=6) if len(raw) >= _GZIP_MIN_BYTES else None


def respond(request: Request, bodies: Bodies) -> Response:
    raw, gz = bodies
    headers = {"Vary": "Accept-Encoding"}
    if gz is not None and "gzip" in request.headers.get("accept-encoding", "").lower():
        headers["Content-Encoding"] = "gzip"
        return Response(content=gz, media_type="application/json", headers=headers)
    return Response(content=raw, media_type="application/json", headers=headers)


def gzip_json(request: Request, payload: Dict) -> Response:
    return respond(request, encode(payload))
