"""The pure-ASGI middlewares. BaseHTTPMiddleware wraps the response in an anyio
stream, which would buffer the /watch body and stall playback until the slowest
scraper finished; these touch only the response start message."""

import time

from core import lumi, metrics, request_id


class RequestContextMiddleware:
    """Tags each request with an id and records its HTTP metrics.

    The id comes from ``X-Request-ID`` when a reverse proxy set one, is bound for
    every log line, and is echoed back so a user can quote it. Latency is
    measured to the response headers, so /watch's long stream does not swamp the
    histogram. Outermost, so a request the login wall rejects still gets an id
    and a count; those 401s land in the unmatched bucket, which keeps
    unauthenticated traffic from minting label values."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        rid = ""
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                rid = request_id.clean(value.decode("latin-1"))
                break
        rid = rid or request_id.new()
        scope.setdefault("state", {})["request_id"] = rid
        token = request_id.bind(rid)

        method = metrics.method_label(scope.get("method"))
        started = time.monotonic()
        metrics.track_in_progress(method, 1)
        recorded = False

        async def send_wrapper(message):
            nonlocal recorded
            if message["type"] == "http.response.start" and not recorded:
                recorded = True
                message.setdefault("headers", []).append((b"x-request-id", rid.encode("latin-1")))
                # The router has set the route by now; a 404 has none and lands
                # in one bucket.
                metrics.record_http_request(
                    method, metrics.route_label(scope), message.get("status", 0), time.monotonic() - started,
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if not recorded:
                # Nothing started: the client hung up or the app raised first.
                # 499 is nginx's client-closed-request.
                metrics.record_http_request(method, metrics.route_label(scope), 499, time.monotonic() - started)
            metrics.track_in_progress(method, -1)
            request_id.unbind(token)


class LumiHeaderMiddleware:
    """``X-Lumi`` carries a rotating quip and ``X-Powered-By`` names the empress.
    A quip that fails to encode is dropped rather than breaking the response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                try:
                    headers.append((b"x-lumi", lumi.header_quip().encode("latin-1")))
                except UnicodeEncodeError:
                    pass
                headers.append((b"x-powered-by", f"{lumi.EMPRESS}, {lumi.TITLE}".encode("latin-1")))
            await send(message)

        await self.app(scope, receive, send_wrapper)
