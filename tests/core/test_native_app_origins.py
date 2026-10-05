from fastapi.testclient import TestClient

from api import app


def _preflight(origin):
    return TestClient(app).options(
        "/config",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )


def test_mobile_app_origins_pass_cors_preflight():
    for origin in [
        "https://crimsonhaven.localhost",
        "https://b-0123abcd4567.crimsonhaven.localhost",
        "capacitor://b-0123abcd4567.crimsonhaven.localhost",
    ]:
        assert _preflight(origin).headers.get("access-control-allow-origin") == origin


def test_lookalike_origins_still_rejected():
    for origin in [
        "https://evil.example",
        "https://crimsonhaven.localhost.evil.example",
        "https://evilcrimsonhaven.localhost",
        "http://crimsonhaven.localhost",
    ]:
        assert "access-control-allow-origin" not in _preflight(origin).headers
