"""The cache's cacheability gate (cache_engine/downloader.py).

The worker re-fetches a stream through our own ``/{source}_proxy`` over loopback,
so only those URL shapes are cacheable. Raw CDN links and crimson-proxy edge links
that the client delivers itself must be refused: pulling them from the backend
fails with 403/429.
"""


from cache_engine.downloader import (
    _is_loopback_proxy_url,
    _media_url_for_stream,
    _to_internal,
)

from core.config import get_settings


def test_same_origin_proxy_urls_are_loopback_pullable():
    for url in (
        "https://api.crimson.to/overlay_proxy?u=x&s=y",   # overlay client-offload subtitle proxy
        "/jellyfin_proxy/Videos/1/master.m3u8",           # Jellyfin (relative)
        "https://api.crimson.to/cache_proxy/abc",         # cache output
        "https://api.crimson.to/voe_proxy?u=x",           # VOE (private overlay)
        "https://api.crimson.to/player?src=%2Fx_proxy",   # /player iframe wrapper
    ):
        assert _is_loopback_proxy_url(url) is True, url


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


def test_to_internal_moves_a_proxy_url_onto_loopback_keeping_the_query():
    internal = _to_internal("https://api.crimson.to/overlay_proxy?u=x&s=y")
    assert internal == f"{get_settings().cache_internal_base}/overlay_proxy?u=x&s=y"


def test_player_iframe_media_url_is_the_same_origin_src():
    # A /player?src=/x_proxy iframe resolves to its same-origin proxy src, which is
    # itself loopback-pullable, so these stay cacheable.
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
