"""Live TV (iptv_engine) — catalogue join, signed proxy links, playlist rewrite.

Pure logic only (no network): build_catalog gets fixture payloads shaped like
the iptv-org API, signing is deterministic under conftest's PROXY_SECRET, and
the m3u8 rewriter is exercised on master + media playlists.
"""

from urllib.parse import parse_qs, urlparse

from iptv_engine.service import (
    IptvService,
    build_catalog,
    is_playlist,
    proxy_path,
    rewrite_playlist,
    sign_stream,
    verify_stream_sig,
)


# --- fixtures shaped like the iptv-org API -----------------------------------
CHANNELS = [
    {"id": "AlphaTV.us", "name": "Alpha TV", "alt_names": ["Alpha"], "network": "AlphaNet",
     "country": "US", "categories": ["news"], "is_nsfw": False, "closed": None,
     "replaced_by": None, "website": "https://alpha.example"},
    {"id": "BetaKids.de", "name": "Beta Kids", "alt_names": [], "network": None,
     "country": "DE", "categories": ["kids", "family"], "is_nsfw": False, "closed": None,
     "replaced_by": None, "website": None},
    {"id": "Naughty.xx", "name": "Naughty", "alt_names": [], "network": None,
     "country": "US", "categories": ["xxx"], "is_nsfw": True, "closed": None,
     "replaced_by": None, "website": None},
    {"id": "Dead.fr", "name": "Dead", "alt_names": [], "network": None,
     "country": "FR", "categories": ["general"], "is_nsfw": False, "closed": "2020-01-01",
     "replaced_by": None, "website": None},
    {"id": "Blocked.us", "name": "Blocked", "alt_names": [], "network": None,
     "country": "US", "categories": ["news"], "is_nsfw": False, "closed": None,
     "replaced_by": None, "website": None},
    {"id": "NoStreams.jp", "name": "No Streams", "alt_names": [], "network": None,
     "country": "JP", "categories": ["general"], "is_nsfw": False, "closed": None,
     "replaced_by": None, "website": None},
]

STREAMS = [
    {"channel": "AlphaTV.us", "url": "https://cdn.example/alpha/480.m3u8",
     "quality": "480p", "label": None, "referrer": None, "user_agent": None},
    {"channel": "AlphaTV.us", "url": "https://cdn.example/alpha/1080.m3u8",
     "quality": "1080p", "label": None, "referrer": "https://alpha.example/", "user_agent": None},
    {"channel": "BetaKids.de", "url": "http://cdn.example/beta.m3u8",
     "quality": None, "label": "Not 24/7", "referrer": None, "user_agent": None},
    {"channel": "Naughty.xx", "url": "https://cdn.example/n.m3u8",
     "quality": "720p", "label": None, "referrer": None, "user_agent": None},
    {"channel": "Blocked.us", "url": "https://cdn.example/blocked.m3u8",
     "quality": "720p", "label": None, "referrer": None, "user_agent": None},
    {"channel": "Dead.fr", "url": "https://cdn.example/dead.m3u8",
     "quality": "720p", "label": None, "referrer": None, "user_agent": None},
    {"channel": None, "url": "https://cdn.example/orphan.m3u8",
     "quality": "720p", "label": None, "referrer": None, "user_agent": None},
]

CATEGORIES = [
    {"id": "news", "name": "News"},
    {"id": "kids", "name": "Kids"},
    {"id": "family", "name": "Family"},
    {"id": "general", "name": "General"},
    {"id": "xxx", "name": "XXX"},
]

COUNTRIES = [
    {"code": "US", "name": "United States", "flag": "🇺🇸"},
    {"code": "DE", "name": "Germany", "flag": "🇩🇪"},
    {"code": "JP", "name": "Japan", "flag": "🇯🇵"},
    {"code": "FR", "name": "France", "flag": "🇫🇷"},
]

LOGOS = [
    {"channel": "AlphaTV.us", "feed": "SD", "in_use": True, "width": 100,
     "url": "https://img.example/alpha-feed.png"},
    {"channel": "AlphaTV.us", "feed": None, "in_use": True, "width": 512,
     "url": "https://img.example/alpha.png"},
    {"channel": "AlphaTV.us", "feed": None, "in_use": False, "width": 1024,
     "url": "https://img.example/alpha-old.png"},
]

BLOCKLIST = [{"channel": "Blocked.us", "reason": "dmca", "ref": "https://x"}]


def _catalog(**kw):
    return build_catalog(CHANNELS, STREAMS, CATEGORIES, COUNTRIES, LOGOS, BLOCKLIST, **kw)


def _service_with_catalog(cat) -> IptvService:
    svc = IptvService()
    svc._catalog = cat
    svc._fetched_at = 1.0
    return svc


# --- catalogue join -----------------------------------------------------------
def test_catalog_membership():
    cat = _catalog()
    ids = set(cat["channels"])
    # In: alive + permitted + playable. Out: NSFW, closed, blocklisted, streamless.
    assert ids == {"AlphaTV.us", "BetaKids.de"}


def test_catalog_includes_nsfw_when_opted_in():
    cat = _catalog(include_nsfw=True)
    assert "Naughty.xx" in cat["channels"]


def test_streams_sorted_best_quality_first_and_untagged_last():
    cat = _catalog()
    alpha = cat["channels"]["AlphaTV.us"]["streams"]
    assert [s["quality"] for s in alpha] == ["1080p", "480p"]
    beta = cat["channels"]["BetaKids.de"]["streams"]
    assert beta[0]["quality"] is None  # untagged still present, just unranked


def test_best_logo_prefers_in_use_channel_level():
    cat = _catalog()
    assert cat["channels"]["AlphaTV.us"]["logo"] == "https://img.example/alpha.png"


def test_facet_counts_reflect_only_surfaced_channels():
    cat = _catalog()
    counts = {c["id"]: c["count"] for c in cat["categories"]}
    # Blocked.us (news) and Dead.fr (general) must not count.
    assert counts == {"news": 1, "kids": 1, "family": 1}
    country_counts = {c["code"]: c["count"] for c in cat["countries"]}
    assert country_counts == {"US": 1, "DE": 1}
    assert {c["code"]: c["flag"] for c in cat["countries"]}["DE"] == "🇩🇪"


# --- listing / search / paging -------------------------------------------------
def test_list_channels_filters_and_search():
    svc = _service_with_catalog(_catalog())
    assert [c["id"] for c in svc.list_channels()["channels"]] == ["AlphaTV.us", "BetaKids.de"]
    assert [c["id"] for c in svc.list_channels(country="de")["channels"]] == ["BetaKids.de"]
    assert [c["id"] for c in svc.list_channels(category="NEWS")["channels"]] == ["AlphaTV.us"]
    # Search hits name, alt_names and network, case-insensitively.
    assert svc.list_channels(q="alphanet")["total"] == 1
    assert svc.list_channels(q="zzz")["total"] == 0


def test_list_channels_paging():
    svc = _service_with_catalog(_catalog())
    page = svc.list_channels(page=2, page_size=1)
    assert page["total"] == 2
    assert [c["id"] for c in page["channels"]] == ["BetaKids.de"]


def test_channel_cards_carry_no_private_fields():
    svc = _service_with_catalog(_catalog())
    card = svc.list_channels()["channels"][0]
    assert "_search" not in card and "streams" not in card
    assert card["best_quality"] == "1080p" and card["stream_count"] == 2


def test_get_channel_detail_signs_streams():
    svc = _service_with_catalog(_catalog())
    detail = svc.get_channel("AlphaTV.us")
    assert detail["name"] == "Alpha TV"
    top = detail["streams"][0]
    assert top["direct_url"] == "https://cdn.example/alpha/1080.m3u8"
    q = parse_qs(urlparse(top["proxy_path"]).query)
    assert verify_stream_sig(q["u"][0], q["s"][0], q.get("r", [""])[0], q.get("a", [""])[0])
    assert svc.get_channel("Blocked.us") is None


def test_direct_ok_marks_browser_playable_streams():
    svc = _service_with_catalog(_catalog())
    alpha = svc.get_channel("AlphaTV.us")["streams"]
    # 1080p is referrer-gated (browser can't send it) -> proxy-only.
    assert alpha[0]["direct_ok"] is False
    # 480p is https with no header demands -> direct-eligible.
    assert alpha[1]["direct_ok"] is True
    # BetaKids is plain http -> mixed content on an https page -> proxy-only.
    beta = svc.get_channel("BetaKids.de")["streams"]
    assert beta[0]["direct_ok"] is False


# --- signing -------------------------------------------------------------------
def test_signature_covers_url_and_header_overrides():
    url = "https://cdn.example/live.m3u8"
    sig = sign_stream(url, "https://ref.example/", "")
    assert verify_stream_sig(url, sig, "https://ref.example/", "")
    # Tampering with the URL or the header overrides invalidates the signature.
    assert not verify_stream_sig(url + "x", sig, "https://ref.example/", "")
    assert not verify_stream_sig(url, sig, "https://evil.example/", "")
    assert not verify_stream_sig(url, sig, "https://ref.example/", "curl/8")
    assert not verify_stream_sig(url, "", "https://ref.example/", "")


def test_proxy_path_omits_absent_header_params():
    plain = proxy_path("https://cdn.example/live.m3u8")
    assert "r=" not in plain and "a=" not in plain
    gated = proxy_path("https://cdn.example/live.m3u8", "https://ref.example/", "UA")
    assert "r=" in gated and "a=" in gated


# --- playlist rewriting ----------------------------------------------------------
def test_rewrite_media_playlist_relative_segments():
    base = "https://cdn.example/live/stream.m3u8"
    text = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg001.ts\n#EXTINF:6.0,\n/abs/seg002.ts\n"
    out = rewrite_playlist(text, base)
    lines = out.splitlines()
    assert lines[0] == "#EXTM3U"  # tags without URIs untouched
    q1 = parse_qs(urlparse(lines[3]).query)
    assert q1["u"][0] == "https://cdn.example/live/seg001.ts"
    assert verify_stream_sig(q1["u"][0], q1["s"][0])
    q2 = parse_qs(urlparse(lines[5]).query)
    assert q2["u"][0] == "https://cdn.example/abs/seg002.ts"


def test_rewrite_master_playlist_uri_attributes_and_header_propagation():
    base = "https://cdn.example/master.m3u8"
    ref, ua = "https://ref.example/", "SpecialUA"
    text = (
        '#EXTM3U\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",URI="audio/index.m3u8"\n'
        '#EXT-X-KEY:METHOD=AES-128,URI="https://keys.example/k.bin",IV=0xABCD\n'
        '#EXT-X-STREAM-INF:BANDWIDTH=800000\n'
        'variants/720.m3u8\n'
    )
    out = rewrite_playlist(text, base, ref, ua)
    assert 'URI="/iptv_proxy?u=' in out
    # Every rewritten URI carries the same signed referrer/UA so segments and
    # keys are fetched with the headers the stream demands.
    for line in out.splitlines():
        if "/iptv_proxy?" in line:
            frag = line.split('URI="', 1)[-1].rstrip('"') if line.startswith("#") else line
            q = parse_qs(urlparse(frag.split('",', 1)[0].rstrip('"')).query)
            assert q["r"][0] == ref and q["a"][0] == ua
            assert verify_stream_sig(q["u"][0], q["s"][0], ref, ua)


def test_playlist_detection():
    assert is_playlist("application/vnd.apple.mpegurl", "https://x/seg.ts")
    assert is_playlist("application/octet-stream", "https://x/live.m3u8?token=1")
    assert not is_playlist("video/mp2t", "https://x/seg.ts")
