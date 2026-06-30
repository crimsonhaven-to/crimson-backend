"""External crimson-proxy signing + host selection (resolvers/_crimson_proxy.py).

The signature contract is shared byte-for-byte with the crimson-proxy repo, so a
silent change here = every offloaded stream 401s. These tests pin the signature
shape, the enable-gating, and the health-aware failover.
"""

import importlib

import pytest

import resolvers._crimson_proxy as cp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Each test sets exactly the env it needs; start from a known-empty state and
    # a fresh health cache so failover assertions are deterministic.
    for var in ("CRIMSON_PROXY_BASE", "PROXY_SECRET", "CRIMSON_PROXY_SOURCES"):
        monkeypatch.delenv(var, raising=False)
    cp._health.clear()
    yield
    cp._health.clear()


def test_signed_query_is_stable_and_covers_all_fields(monkeypatch):
    monkeypatch.setenv("PROXY_SECRET", "shared-secret")
    q = cp._signed_query("https://cdn/x.m3u8", "https://ref/", "https://orig", "UA/1")
    # url/referer/origin/ua all present, plus a 32-char hex signature.
    assert "u=https%3A%2F%2Fcdn%2Fx.m3u8" in q
    assert "r=https%3A%2F%2Fref%2F" in q
    assert "o=https%3A%2F%2Forig" in q
    assert "ua=UA%2F1" in q
    sig = dict(p.split("=", 1) for p in q.split("&"))["s"]
    assert len(sig) == 32 and all(c in "0123456789abcdef" for c in sig)


def test_signature_matches_documented_hmac(monkeypatch):
    # Re-derive the signature the documented way (HMAC-SHA256 over the
    # newline-joined fields, hex[:32]) and assert the module agrees.
    import hashlib
    import hmac

    monkeypatch.setenv("PROXY_SECRET", "shared-secret")
    url, ref, orig, ua = "https://cdn/x.m3u8", "https://ref/", "", ""
    expected = hmac.new(
        b"shared-secret", "\n".join([url, ref, orig, ua]).encode(), hashlib.sha256
    ).hexdigest()[:32]
    q = cp._signed_query(url, ref, orig, ua)
    assert dict(p.split("=", 1) for p in q.split("&"))["s"] == expected


def test_signature_changes_with_secret(monkeypatch):
    monkeypatch.setenv("PROXY_SECRET", "secret-a")
    a = cp._signed_query("https://cdn/x", "", "", "")
    monkeypatch.setenv("PROXY_SECRET", "secret-b")
    b = cp._signed_query("https://cdn/x", "", "", "")
    assert a != b


def test_is_enabled_requires_base_and_secret(monkeypatch):
    assert cp.is_enabled() is False  # nothing set
    monkeypatch.setenv("CRIMSON_PROXY_BASE", "https://edge.example")
    assert cp.is_enabled() is False  # base but no secret
    monkeypatch.setenv("PROXY_SECRET", "s")
    assert cp.is_enabled() is True


def test_is_enabled_honours_per_source_allowlist(monkeypatch):
    monkeypatch.setenv("CRIMSON_PROXY_BASE", "https://edge.example")
    monkeypatch.setenv("PROXY_SECRET", "s")
    monkeypatch.setenv("CRIMSON_PROXY_SOURCES", "cinema.bz")
    assert cp.is_enabled("cinema.bz") is True
    assert cp.is_enabled("PlayIMDb") is False  # not in the allowlist
    assert cp.is_enabled() is True  # global check ignores the allowlist


def test_proxy_bases_parses_comma_list_and_strips_slashes(monkeypatch):
    monkeypatch.setenv("CRIMSON_PROXY_BASE", " https://a.example/ , https://b.example ")
    assert cp.proxy_bases() == ["https://a.example", "https://b.example"]


def test_proxy_url_routes_only_to_healthy_hosts(monkeypatch):
    monkeypatch.setenv("CRIMSON_PROXY_BASE", "https://up.example,https://down.example")
    monkeypatch.setenv("PROXY_SECRET", "s")
    import time

    now = time.time()
    cp._health["https://up.example"] = {"healthy": True, "ts": now}
    cp._health["https://down.example"] = {"healthy": False, "ts": now}
    # With one host known-healthy, every minted link must target it.
    for _ in range(20):
        assert cp.proxy_url("https://cdn/x.m3u8").startswith("https://up.example/?")


def test_proxy_url_falls_back_to_all_when_health_unknown(monkeypatch):
    # Cold cache => degrade to "try anything" rather than giving up.
    monkeypatch.setenv("CRIMSON_PROXY_BASE", "https://only.example")
    monkeypatch.setenv("PROXY_SECRET", "s")
    assert cp.proxy_url("https://cdn/x.m3u8").startswith("https://only.example/?")


def test_module_reimports_cleanly():
    # Guard against import-time side effects creeping in.
    importlib.reload(cp)
