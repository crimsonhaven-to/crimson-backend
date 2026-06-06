"""
Jellyfin source — authenticated, ad-free, proxied.

Streams anime straight from the user's own Jellyfin server. Configured purely
via env (see .env.example):

    JELLYFIN_URL=https://jellyfin.example.com      # reachable from the backend
    JELLYFIN_USERNAME=crimson
    JELLYFIN_PASSWORD=...                           # may be empty

Flow (mirrors the Movish / VidKing Test proxy pattern):

    scraper  -> finds the Series by TMDB id (ProviderIds) / title, then the
                episode item, and emits  crimson-jellyfin:{itemId}
    resolver -> POST /Items/{itemId}/PlaybackInfo with a browser DeviceProfile
                to pick playback mode (Auto):
                  * mp4/webm + web codecs        -> direct file  (type "mp4")
                  * anything else (MKV, HEVC, …) -> Jellyfin HLS (type "hls",
                    remux/transcode)
                and returns a RELATIVE /jellyfin_proxy/... path.
    api.py   -> prefixes the request base URL so the frontend gets an absolute
                stream URL, and serves the proxy route.

The proxy (``proxy_fetch`` below) injects the Jellyfin access token **server
side** on every upstream request and strips it from the HLS playlists it serves,
so the token never reaches the browser. Because everything is fetched by the
backend, the Jellyfin server can stay private/LAN-only and needs no CORS.

See [[movish-player-internals]] for the proxy pattern this mirrors.
"""

import asyncio
import os
import re
from typing import AsyncIterator, Optional, Tuple, Union
from urllib.parse import parse_qsl, urlencode, urlparse

import httpx

from .base_resolver import BaseResolver

# --- Proxy routing ---------------------------------------------------------
PROXY_PREFIX = "/jellyfin_proxy"
# The scraper emits "crimson-jellyfin:{itemId}"; the resolver matches on this.
EMBED_MARKER = "crimson-jellyfin"

# Device identity Jellyfin ties the session/token to. Stable so transcode
# sessions stay coherent across the master/variant/segment requests.
_CLIENT = "Crimson"
_DEVICE = "Crimson Backend"
_DEVICE_ID = "crimson-backend"
_VERSION = "2.0"

# Browser playback profile handed to Jellyfin's PlaybackInfo. DirectPlay is
# allowed only for what a <video> element can actually play (mp4/webm + web
# codecs); everything else is steered to HLS (h264/aac in MPEG-TS), so MKV /
# HEVC / etc. transcode-or-remux instead of returning an unplayable file.
DEVICE_PROFILE = {
    "MaxStreamingBitrate": 120000000,
    "MaxStaticBitrate": 100000000,
    "DirectPlayProfiles": [
        {
            "Container": "mp4,m4v,mov",
            "Type": "Video",
            "VideoCodec": "h264,vp8,vp9,av1",
            "AudioCodec": "aac,mp3,opus,flac,vorbis",
        },
        {"Container": "webm", "Type": "Video", "VideoCodec": "vp8,vp9,av1", "AudioCodec": "opus,vorbis"},
    ],
    "TranscodingProfiles": [
        {
            "Container": "ts",
            "Type": "Video",
            "VideoCodec": "h264",
            "AudioCodec": "aac,mp3",
            "Protocol": "hls",
            "Context": "Streaming",
            "MaxAudioChannels": "2",
            "MinSegments": "1",
            "BreakOnNonKeyFrames": True,
        }
    ],
    "ContainerProfiles": [],
    "CodecProfiles": [],
    "SubtitleProfiles": [
        {"Format": "vtt", "Method": "External"},
        {"Format": "srt", "Method": "External"},
        {"Format": "ass", "Method": "External"},
    ],
}

# Containers / codecs a browser can play directly in a <video> tag.
_WEB_CONTAINERS = {"mp4", "m4v", "mov", "webm"}
_WEB_VCODECS = {"h264", "avc1", "vp8", "vp9", "av1"}
_WEB_ACODECS = {"aac", "mp3", "opus", "vorbis", "flac", ""}


# --- Config (read lazily; api.py calls load_dotenv() AFTER importing us) ----
def get_config() -> Tuple[str, str, str]:
    url = (os.getenv("JELLYFIN_URL") or "").rstrip("/")
    user = os.getenv("JELLYFIN_USERNAME") or ""
    pw = os.getenv("JELLYFIN_PASSWORD")
    return url, user, ("" if pw is None else pw)


def is_configured() -> bool:
    url, user, _ = get_config()
    return bool(url and user)


# --- Auth ------------------------------------------------------------------
_token: Optional[str] = None
_user_id: Optional[str] = None
_auth_lock = asyncio.Lock()


def _auth_header(token: Optional[str] = None) -> str:
    parts = [
        f'MediaBrowser Client="{_CLIENT}"',
        f'Device="{_DEVICE}"',
        f'DeviceId="{_DEVICE_ID}"',
        f'Version="{_VERSION}"',
    ]
    if token:
        parts.append(f'Token="{token}"')
    return ", ".join(parts)


async def authenticate() -> Tuple[str, str]:
    """(Re)authenticate with username+password; cache token + user id."""
    global _token, _user_id
    url, user, pw = get_config()
    if not (url and user):
        raise RuntimeError("Jellyfin not configured")

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{url}/Users/AuthenticateByName",
            headers={"Authorization": _auth_header(), "Content-Type": "application/json"},
            json={"Username": user, "Pw": pw},
        )
    resp.raise_for_status()
    data = resp.json()
    _token = data.get("AccessToken")
    _user_id = (data.get("User") or {}).get("Id")
    if not _token or not _user_id:
        raise RuntimeError("Jellyfin auth returned no token / user id")
    print(f"[Jellyfin] Authenticated as '{user}' (user {_user_id}).")
    return _token, _user_id


async def _ensure_auth() -> Tuple[str, str]:
    if _token and _user_id:
        return _token, _user_id
    async with _auth_lock:
        if _token and _user_id:
            return _token, _user_id
        return await authenticate()


async def api_request(
    method: str, path: str, params: Optional[dict] = None, json_body: Optional[dict] = None
) -> httpx.Response:
    """Authenticated Jellyfin API call; re-auths once on a 401."""
    url, _, _ = get_config()
    token, _uid = await _ensure_auth()

    async def _do(tok: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            return await client.request(
                method,
                f"{url}{path}",
                headers={"Authorization": _auth_header(tok), "Content-Type": "application/json"},
                params=params,
                json=json_body,
            )

    resp = await _do(token)
    if resp.status_code == 401:
        token, _uid = await authenticate()
        resp = await _do(token)
    resp.raise_for_status()
    return resp


async def api_get(path: str, params: Optional[dict] = None) -> dict:
    return (await api_request("GET", path, params=params)).json()


# --- HLS playlist / URL rewriting ------------------------------------------
def _strip_api_key(url: str) -> str:
    """Remove any api_key/ApiKey query param so the token isn't exposed."""
    low = url.lower()
    if ("api_key" not in low and "apikey" not in low) or "?" not in url:
        return url
    base, q = url.split("?", 1)
    pairs = [(k, v) for k, v in parse_qsl(q, keep_blank_values=True) if k.lower() not in ("api_key", "apikey")]
    return base + ("?" + urlencode(pairs) if pairs else "")


def _route_through_proxy(url: str, jellyfin_url: str) -> str:
    """Point a playlist URL at our same-origin proxy and drop its token.

    Relative URLs are left untouched — they resolve under the proxy path the
    playlist is already served from. Absolute Jellyfin URLs (and root-absolute
    paths) are rewritten to the proxy prefix.
    """
    ju = urlparse(jellyfin_url)
    if url.startswith(jellyfin_url):
        url = PROXY_PREFIX + url[len(jellyfin_url):]
    elif url.startswith(("http://", "https://")):
        p = urlparse(url)
        if p.netloc == ju.netloc:  # same Jellyfin host, different scheme/base
            url = PROXY_PREFIX + p.path + (("?" + p.query) if p.query else "")
        else:
            return _strip_api_key(url)  # genuinely foreign host — leave it
    elif url.startswith("/"):
        url = PROXY_PREFIX + url
    # else: relative path -> resolves under the proxy already.
    return _strip_api_key(url)


def rewrite_playlist(text: str, jellyfin_url: str) -> str:
    """Rewrite an HLS m3u8 so every sub-resource flows through the proxy."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out.append(line)
        elif stripped.startswith("#"):
            # URI="..." appears in EXT-X-MEDIA / EXT-X-KEY / EXT-X-I-FRAME-STREAM-INF.
            out.append(
                re.sub(
                    r'URI="([^"]+)"',
                    lambda m: 'URI="' + _route_through_proxy(m.group(1), jellyfin_url) + '"',
                    line,
                )
            )
        else:
            out.append(_route_through_proxy(stripped, jellyfin_url))
    return "\n".join(out)


def is_rewritable(content_type: str) -> bool:
    """True only for HLS playlists (the one text resource we rewrite)."""
    return "mpegurl" in (content_type or "").lower()


# --- The proxy fetch (used by the api.py route) ----------------------------
async def proxy_fetch(
    path: str,
    query_string: str = "",
    method: str = "GET",
    body: Optional[bytes] = None,
    range_header: Optional[str] = None,
) -> Tuple[int, str, dict, Union[bytes, AsyncIterator[bytes]]]:
    """
    Fetch ``{JELLYFIN_URL}/{path}?{query_string}`` with the token injected, and
    return ``(status_code, content_type, forward_headers, body)``.

    ``body`` is ``bytes`` for HLS playlists (token-stripped + URL-rewritten) and
    an async byte-iterator for media segments / direct files (streamed straight
    through with Range passthrough). Raises ``ValueError`` if Jellyfin isn't
    configured.
    """
    if not is_configured():
        raise ValueError("Jellyfin not configured")
    jellyfin_url, _, _ = get_config()

    def _build_query(token: str) -> str:
        qs = query_string or ""
        if "api_key=" not in qs.lower() and "apikey=" not in qs.lower():
            qs = (qs + "&" if qs else "") + "api_key=" + token
        return qs

    async def _open(token: str):
        upstream = f"{jellyfin_url}/{path.lstrip('/')}?{_build_query(token)}"
        req_headers = {"Authorization": _auth_header(token)}
        if range_header:
            req_headers["Range"] = range_header
        client = httpx.AsyncClient(
            follow_redirects=True, timeout=httpx.Timeout(30.0, read=None)
        )
        req = client.build_request(method, upstream, content=body)
        resp = await client.send(req, stream=True)
        return client, resp

    token, _uid = await _ensure_auth()
    client, resp = await _open(token)
    if resp.status_code == 401:
        await resp.aclose()
        await client.aclose()
        token, _uid = await authenticate()
        client, resp = await _open(token)

    content_type = resp.headers.get("content-type", "application/octet-stream")

    if is_rewritable(content_type):
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        text = rewrite_playlist(raw.decode("utf-8", errors="replace"), jellyfin_url)
        return resp.status_code, content_type, {}, text.encode("utf-8")

    # Media / segment: stream straight through, forwarding Range headers.
    forward = {
        h: resp.headers[h]
        for h in ("content-range", "accept-ranges", "content-length", "cache-control")
        if h in resp.headers
    }

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return resp.status_code, content_type, forward, body_iter()


# --- Playback decision -----------------------------------------------------
async def playback_info(item_id: str) -> dict:
    """Ask Jellyfin how this item should be played for our browser profile."""
    _, uid = await _ensure_auth()
    resp = await api_request(
        "POST",
        f"/Items/{item_id}/PlaybackInfo",
        params={"userId": uid},
        json_body={
            "DeviceProfile": DEVICE_PROFILE,
            "MaxStreamingBitrate": 120000000,
            "AutoOpenLiveStream": False,
        },
    )
    return resp.json()


def _codec_of(media_source: dict, stream_type: str) -> str:
    for s in media_source.get("MediaStreams") or []:
        if s.get("Type") == stream_type:
            return (s.get("Codec") or "").lower()
    return ""


def _is_web_playable(media_source: dict) -> bool:
    """Auto decision: can a <video> tag direct-play this file as-is?"""
    containers = {c.strip().lower() for c in (media_source.get("Container") or "").split(",")}
    if not (containers & _WEB_CONTAINERS):
        return False
    return _codec_of(media_source, "Video") in _WEB_VCODECS and _codec_of(media_source, "Audio") in _WEB_ACODECS


class JellyfinResolver(BaseResolver):
    """
    Resolves a ``crimson-jellyfin:{itemId}`` marker to a backend-proxied stream
    URL. Auto-selects direct-play (mp4) vs HLS (transcode/remux) per episode.

    ``resolve`` returns a *relative* ``/jellyfin_proxy/...`` path; ``api.py``
    prefixes it with the request base URL so the frontend gets an absolute URL.
    """

    domain_keyword: str = EMBED_MARKER
    source_name: str = "Jellyfin"

    async def resolve(self, embed_url: str) -> Optional[str]:
        if not is_configured():
            return None

        item_id = embed_url.split(":", 1)[1] if ":" in embed_url else embed_url
        item_id = item_id.strip().strip("/")
        if not item_id:
            return None

        print(f"[JellyfinResolver] Resolving item {item_id}")
        try:
            info = await playback_info(item_id)
        except Exception as e:
            print(f"[JellyfinResolver] PlaybackInfo failed: {type(e).__name__} - {e}")
            return None

        sources = info.get("MediaSources") or []
        if not sources:
            print("[JellyfinResolver] No media sources for item.")
            return None

        ms = sources[0]
        ms_id = ms.get("Id") or item_id
        play_session = info.get("PlaySessionId") or ""

        # Auto: direct-play browser-friendly files; everything else -> HLS.
        if _is_web_playable(ms):
            params = {"static": "true", "mediaSourceId": ms_id}
            if play_session:
                params["playSessionId"] = play_session
            proxy_path = f"{PROXY_PREFIX}/Videos/{item_id}/stream?{urlencode(params)}"
            print(f"[JellyfinResolver] SUCCESS (direct): {proxy_path}")
            return proxy_path

        # HLS: prefer Jellyfin's ready-made TranscodingUrl (has all the right
        # params); fall back to a hand-built master.m3u8 request.
        transcode_url = ms.get("TranscodingUrl")
        if transcode_url:
            proxy_path = _route_through_proxy(transcode_url, get_config()[0])
            print(f"[JellyfinResolver] SUCCESS (hls/transcode): {proxy_path}")
            return proxy_path

        params = {
            "mediaSourceId": ms_id,
            "videoCodec": "h264",
            "audioCodec": "aac,mp3",
            "container": "ts",
            "transcodingProtocol": "hls",
            "transcodingContainer": "ts",
            "maxAudioChannels": "2",
        }
        if play_session:
            params["playSessionId"] = play_session
        proxy_path = f"{PROXY_PREFIX}/Videos/{item_id}/master.m3u8?{urlencode(params)}"
        print(f"[JellyfinResolver] SUCCESS (hls/fallback): {proxy_path}")
        return proxy_path
