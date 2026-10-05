from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import get_settings
from system_engine import routes


def _config():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app).get("/config").json()


def test_config_says_whether_the_desktop_app_is_served(monkeypatch):
    monkeypatch.delenv("APP_RELEASES_URL", raising=False)
    get_settings.cache_clear()
    assert _config()["desktop_app"] is False
    monkeypatch.setenv("APP_RELEASES_URL", "https://releases.test/pkg")
    get_settings.cache_clear()
    assert _config()["desktop_app"] is True
