"""Cache-engine cacheability gate (cache_engine/downloader.py).

The cache remuxes a stream by re-fetching it through the backend's OWN
``/{source}_proxy`` route over loopback (``_to_internal``). That only works for
same-origin proxy URLs — so since the E2/E3 offload landed, ``/watch`` also
surfaces streams the client delivers itself (raw CDN links, crimson-proxy edge
links), and pulling those back through the backend is what produced the 429 /
wrong-ASN / edge-rate-limit cache failures.

These tests pin the positive invariant: a stream is cacheable ONLY when its media
URL is one of our loopback-pullable proxy shapes, and the "is it cacheable" test
(``_is_loopback_proxy_url``) and the loopback rewrite (``_to_internal``) never
disagree.
"""

from cache_engine.downloader import (
    _is_loopback_proxy_url,
    _media_url_for_stream,
    _to_internal,
    INTERNAL_BASE,
)


# --- same-origin proxy shapes we CAN pull over loopback --------------------
def test_same_origin_proxy_urls_are_loopback_pullable():
    for url in (
        "https://api.crimson.to/febbox_proxy?u=x&s=y",   # ShowBox / Febbox
        "/jellyfin_proxy/Videos/1/master.m3u8",           # Jellyfin (relative)
        "https://api.crimson.to/cache_proxy/abc",         # cache output
        "https://api.crimson.to/voe_proxy?u=x",           # VOE (private overlay)
        "https://api.crimson.to/player?src=%2Fx_proxy",   # /player iframe wrapper
    ):
        assert _is_loopback_proxy_url(url) is True, url


# --- offloaded / client-delivered shapes we must NOT cache -----------------
def test_offloaded_and_raw_cdn_urls_are_not_loopback_pullable():
    for url in (
        "https://crimson-proxy.netlify.app/?u=https%3A%2F%2Fcdn&s=sig",  # E2 edge
        "https://edge.crimson-proxy.workers.dev/?u=x&s=y",               # E2 edge
        "https://hls.shegu.net/hls/master.m3u8",                          # E3 raw CDN
        "https://delivery.voe-network.net/engine/hls2/x/master.txt",      # raw CDN
    ):
        assert _is_loopback_proxy_url(url) is False, url


def test_malformed_url_is_not_pullable():
    assert _is_loopback_proxy_url("") is False
    assert _is_loopback_proxy_url("not a url") is False


# --- the gate and the rewrite agree by construction ------------------------
def test_to_internal_rewrites_exactly_the_loopback_pullable_urls():
    # Same-origin proxy -> rewritten onto loopback (query preserved).
    internal = _to_internal("https://api.crimson.to/febbox_proxy?u=x&s=y")
    assert internal == f"{INTERNAL_BASE}/febbox_proxy?u=x&s=y"

    # A raw CDN URL is left untouched (and the gate rejects it upstream, so a job
    # like this never actually reaches _to_internal).
    raw = "https://hls.shegu.net/hls/master.m3u8"
    assert _to_internal(raw) == raw

    # Whatever _to_internal chooses to rewrite is exactly what the gate accepts.
    for url in (
        "https://api.crimson.to/febbox_proxy?u=x",
        "https://hls.shegu.net/x.m3u8",
        "https://crimson-proxy.netlify.app/?u=x&s=y",
        "/jellyfin_proxy/Videos/1/master.m3u8",
    ):
        rewritten = _to_internal(url) != url
        assert rewritten == _is_loopback_proxy_url(url), url


# --- media-url extraction still resolves the iframe /player src ------------
def test_player_iframe_media_url_is_the_same_origin_src():
    # A /player?src=/x_proxy iframe resolves to its same-origin proxy src, which is
    # itself loopback-pullable — so these stay cacheable.
    media = _media_url_for_stream({
        "type": "iframe",
        "url": "https://api.crimson.to/player?src=%2Fanimesuge_proxy%3Fu%3Dx%26s%3Dy",
    })
    assert media == "https://api.crimson.to/animesuge_proxy?u=x&s=y"
    assert _is_loopback_proxy_url(media) is True


def test_non_player_iframe_has_no_tappable_media_url():
    # A player-page iframe we don't front (e.g. Movish) has no clean stream to pull.
    assert _media_url_for_stream({
        "type": "iframe",
        "url": "https://api.movish.net/embed/api?id=x",
    }) is None
