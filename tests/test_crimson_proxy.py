"""External crimson-proxy signing + host selection (resolvers/_crimson_proxy.py).

The signature contract is shared byte-for-byte with the crimson-proxy repo, so a
silent change here = every offloaded stream 401s. These tests pin the signature
shape, the enable-gating, and the health-aware failover.
"""

import importlib

import pytest

import resolvers._crimson_proxy as cp
from core.config import Settings, get_settings


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    # Each test sets exactly what it needs; start empty, with a fresh health cache
    # so failover assertions are deterministic.
    settings = get_settings()
    monkeypatch.setattr(settings, "crimson_proxy_base", [])
    monkeypatch.setattr(settings, "proxy_secret", "")
    cp._health.clear()
    yield settings
    cp._health.clear()


def test_signed_query_is_stable_and_covers_all_fields(settings):
    settings.proxy_secret = "shared-secret"
    q = cp._signed_query("https://cdn/x.m3u8", "https://ref/", "https://orig", "UA/1")
    # url/referer/origin/ua all present, plus a 32-char hex signature.
    assert "u=https%3A%2F%2Fcdn%2Fx.m3u8" in q
    assert "r=https%3A%2F%2Fref%2F" in q
    assert "o=https%3A%2F%2Forig" in q
    assert "ua=UA%2F1" in q
    sig = dict(p.split("=", 1) for p in q.split("&"))["s"]
    assert len(sig) == 32 and all(c in "0123456789abcdef" for c in sig)


def test_signature_matches_documented_hmac(settings):
    # Re-derive the signature the documented way (HMAC-SHA256 over the
    # newline-joined fields, hex[:32]) and assert the module agrees.
    import hashlib
    import hmac

    settings.proxy_secret = "shared-secret"
    url, ref, orig, ua = "https://cdn/x.m3u8", "https://ref/", "", ""
    expected = hmac.new(
        b"shared-secret", "\n".join([url, ref, orig, ua]).encode(), hashlib.sha256
    ).hexdigest()[:32]
    q = cp._signed_query(url, ref, orig, ua)
    assert dict(p.split("=", 1) for p in q.split("&"))["s"] == expected


def test_signature_changes_with_secret(settings):
    settings.proxy_secret = "secret-a"
    a = cp._signed_query("https://cdn/x", "", "", "")
    settings.proxy_secret = "secret-b"
    b = cp._signed_query("https://cdn/x", "", "", "")
    assert a != b


def test_is_enabled_requires_base_and_secret(settings):
    assert cp.is_enabled() is False  # nothing set
    settings.crimson_proxy_base = ["https://edge.example"]
    assert cp.is_enabled() is False  # base but no secret
    settings.proxy_secret = "s"
    assert cp.is_enabled() is True


def test_proxy_bases_parse_a_comma_list_and_strip_slashes():
    parsed = Settings(crimson_proxy_base=" https://a.example/ , https://b.example ")
    assert parsed.crimson_proxy_base == ["https://a.example", "https://b.example"]


def test_proxy_url_routes_only_to_healthy_hosts(settings):
    settings.crimson_proxy_base = ["https://up.example", "https://down.example"]
    settings.proxy_secret = "s"
    import time

    now = time.time()
    cp._health["https://up.example"] = {"healthy": True, "ts": now}
    cp._health["https://down.example"] = {"healthy": False, "ts": now}
    # With one host known-healthy, every minted link must target it.
    for _ in range(20):
        assert cp.proxy_url("https://cdn/x.m3u8").startswith("https://up.example/?")


def test_proxy_url_falls_back_to_all_when_health_unknown(settings):
    # Cold cache => degrade to "try anything" rather than giving up.
    settings.crimson_proxy_base = ["https://only.example"]
    settings.proxy_secret = "s"
    assert cp.proxy_url("https://cdn/x.m3u8").startswith("https://only.example/?")


def test_module_reimports_cleanly():
    # Guard against import-time side effects creeping in.
    importlib.reload(cp)
