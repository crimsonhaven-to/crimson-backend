"""The backend's public base URL as the browser sees it."""

from fastapi.requests import Request


def public_base_url(request: Request) -> str:
    """Behind a TLS-terminating proxy uvicorn sees plain HTTP, so trusting
    ``request.base_url`` would emit ``http://`` stream URLs that an HTTPS frontend
    blocks as mixed content. The forwarded headers carry the real scheme and host."""
    proto = request.headers.get("x-forwarded-proto")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if proto and host:
        return f"{proto.split(',')[0].strip()}://{host}/"
    return str(request.base_url)
