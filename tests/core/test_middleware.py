from core.middleware import RequestContextMiddleware


def _receive():
    async def receive():
        return {"type": "http.request"}
    return receive


async def test_body_chunks_pass_straight_through():
    """/watch is progressive: the player renders each source the instant its line
    arrives. A middleware that collects the body would hold every line until the
    slowest scraper finished. Driven over the raw ASGI callable, because an httpx
    transport collects the body itself and could not show this."""
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        for i in range(3):
            await send({"type": "http.response.body", "body": f"line{i}\n".encode(), "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/watch/1/1/1", "headers": []}
    await RequestContextMiddleware(app)(scope, _receive(), send)

    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert [m["body"] for m in bodies] == [b"line0\n", b"line1\n", b"line2\n", b""]
    assert [m["more_body"] for m in bodies] == [True, True, True, False]


async def test_the_inbound_id_is_stamped_on_the_response():
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "GET", "path": "/health", "headers": [(b"x-request-id", b"inbound-id")]}
    await RequestContextMiddleware(app)(scope, _receive(), send)

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert (b"x-request-id", b"inbound-id") in start["headers"]
    assert scope["state"]["request_id"] == "inbound-id"


async def test_non_http_scopes_pass_untouched():
    seen = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    await RequestContextMiddleware(app)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]
