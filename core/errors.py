"""Error responses in Lumi's voice. ``error`` keeps the technical detail the
client may key on; ``message`` is what its banner shows."""

import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core import lumi
from core.config import get_settings

logger = logging.getLogger("crimson.errors")


def _body(status: int, error, **extra) -> dict:
    return {"success": False, "error": error, "message": lumi.voiced_error(status), **extra}


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=_body(exc.status_code, exc.detail, status_code=exc.status_code))


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    detail = str(exc) if get_settings().debug else None
    return JSONResponse(status_code=500, content=_body(500, "Internal server error", detail=detail))


def rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """slowapi's 429, keeping its Retry-After, with the voiced body."""
    base = _rate_limit_exceeded_handler(request, exc)
    retry_after = {k: v for k, v in base.headers.items() if k.lower() == "retry-after"}
    return JSONResponse(
        status_code=429,
        content=_body(429, "Rate limit exceeded", status_code=429),
        headers=retry_after or None,
    )
