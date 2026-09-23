"""The operator's own Jellyfin server as a source.

The scraper finds the episode or movie item and emits ``crimson-jellyfin:{itemId}``.
Here Jellyfin's PlaybackInfo, asked with a browser device profile, decides the
mode: a file a ``<video>`` tag can play goes out as a direct mp4, anything else
(MKV, HEVC and the like) as Jellyfin's HLS remux or transcode. ``/watch`` gets a
path on the token-injecting ``/jellyfin_proxy``; the ``/resolve`` grant gets the
raw token-less Jellyfin URL for the crimson-proxy edge to fetch.

Configured by ``JELLYFIN_URL``, ``JELLYFIN_USERNAME`` and ``JELLYFIN_PASSWORD``.
"""

import logging
from typing import List, Optional, Tuple
from urllib.parse import urlencode

from ._jellyfin_client import _ensure_auth, api_get, api_request, get_config, is_configured
from ._jellyfin_proxy import proxy_fetch, route_through_proxy, strip_api_key
from ._marker import marker_token
from .base_resolver import BaseResolver

__all__ = [
    "EMBED_MARKER",
    "JellyfinResolver",
    "_ensure_auth",
    "api_get",
    "get_config",
    "is_configured",
    "proxy_fetch",
]

logger = logging.getLogger(__name__)

EMBED_MARKER = "crimson-jellyfin"

# Direct play only for what a <video> element plays; everything else is steered
# to HLS (h264/aac in MPEG-TS) so Jellyfin never hands back an unplayable file.
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

_WEB_CONTAINERS = {"mp4", "m4v", "mov", "webm"}
_WEB_VCODECS = {"h264", "avc1", "vp8", "vp9", "av1"}
_WEB_ACODECS = {"aac", "mp3", "opus", "vorbis", "flac", ""}


async def _playback_info(item_id: str) -> dict:
    _, uid = await _ensure_auth()
    resp = await api_request(
        "POST",
        f"/Items/{item_id}/PlaybackInfo",
        params={"userId": uid},
        json_body={"DeviceProfile": DEVICE_PROFILE, "MaxStreamingBitrate": 120000000, "AutoOpenLiveStream": False},
    )
    return resp.json()


def _codec_of(media_source: dict, stream_type: str) -> str:
    for s in media_source.get("MediaStreams") or []:
        if s.get("Type") == stream_type:
            return (s.get("Codec") or "").lower()
    return ""


def _is_web_playable(media_source: dict) -> bool:
    containers = {c.strip().lower() for c in (media_source.get("Container") or "").split(",")}
    return bool(containers & _WEB_CONTAINERS) and (
        _codec_of(media_source, "Video") in _WEB_VCODECS and _codec_of(media_source, "Audio") in _WEB_ACODECS
    )


async def _playback_plan(embed_url: str) -> Optional[Tuple[str, str]]:
    """``(stream_type, upstream_url)`` for an embed: ``mp4`` with the static file
    URL, or ``hls`` with Jellyfin's master playlist. The URL is absolute on
    ``JELLYFIN_URL`` and may still carry Jellyfin's api_key."""
    if not is_configured():
        return None
    item_id = marker_token(embed_url)
    if not item_id:
        return None
    try:
        info = await _playback_info(item_id)
    except Exception as e:
        logger.warning("Jellyfin: PlaybackInfo failed for %s: %s - %s", item_id, type(e).__name__, e)
        return None
    sources = info.get("MediaSources") or []
    if not sources:
        logger.info("Jellyfin: no media sources for item %s", item_id)
        return None

    jellyfin_url, _, _ = get_config()
    ms = sources[0]
    params = {"mediaSourceId": ms.get("Id") or item_id}
    if _is_web_playable(ms):
        params = {"static": "true", **params}
        if info.get("PlaySessionId"):
            params["playSessionId"] = info["PlaySessionId"]
        return "mp4", f"{jellyfin_url}/Videos/{item_id}/stream?{urlencode(params)}"

    # Jellyfin's own TranscodingUrl carries every parameter it wants; the hand-built
    # master.m3u8 is the fallback when it offers none.
    transcode_url = ms.get("TranscodingUrl")
    if transcode_url:
        return "hls", (jellyfin_url + transcode_url) if transcode_url.startswith("/") else transcode_url
    params.update({
        "videoCodec": "h264",
        "audioCodec": "aac,mp3",
        "container": "ts",
        "transcodingProtocol": "hls",
        "transcodingContainer": "ts",
        "maxAudioChannels": "2",
    })
    if info.get("PlaySessionId"):
        params["playSessionId"] = info["PlaySessionId"]
    return "hls", f"{jellyfin_url}/Videos/{item_id}/master.m3u8?{urlencode(params)}"


class JellyfinResolver(BaseResolver):
    domain_keyword: str = EMBED_MARKER
    source_name: str = "Jellyfin"

    async def resolve(self, embed_url: str) -> Optional[str]:
        """A relative ``/jellyfin_proxy`` path, typed mp4 or hls by the pipeline."""
        plan = await _playback_plan(embed_url)
        if plan is None:
            return None
        stream_type, upstream = plan
        path = route_through_proxy(upstream, get_config()[0])
        logger.info("Jellyfin: resolved %s (%s)", embed_url, stream_type)
        return path

    async def resolve_direct(self, embed_url: str) -> List[dict]:
        """The raw Jellyfin URL with no api_key, for the crimson-proxy edge only:
        the edge holds the token and injects it, so the extension, which cannot
        hold it, never gets this grant."""
        plan = await _playback_plan(embed_url)
        if plan is None:
            return []
        stream_type, upstream = plan
        return [{"url": strip_api_key(upstream), "streamType": stream_type}]
