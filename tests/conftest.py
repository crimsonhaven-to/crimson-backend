import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Deterministic values so signing tests do not depend on the host environment.
# setdefault, so a real key in a local .env still wins.
os.environ.setdefault("PROXY_SECRET", "test-proxy-secret")
os.environ.setdefault("TMDB_API_KEY", "test-tmdb-key")

from core.config import get_settings  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_settings():
    """Settings are cached per process, so one test's overrides must not leak
    into the next."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    """No test has a database. Fail fast instead of waiting out the pool's
    connect timeout on a best-effort write."""
    from core import db_pool

    def _unavailable():
        raise RuntimeError("tests run without a database")

    monkeypatch.setattr(db_pool, "get_pool", _unavailable)
