"""The progressive NDJSON responses the player reads, one line per event."""

import json
from typing import AsyncIterator, Dict

from fastapi.responses import StreamingResponse

# nginx buffers by default, which would hold every line until the stream ends.
_STREAM_HEADERS = {"X-Accel-Buffering": "no", "Cache-Control": "no-cache"}


def line(obj: Dict) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def response(lines: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(lines, media_type="application/x-ndjson", headers=_STREAM_HEADERS)
