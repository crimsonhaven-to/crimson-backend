"""OpenSubtitles search, download and SRT to WebVTT conversion for the player.

This lives server-side because the API key must stay secret and ``/download``
is quota-limited per key. Searching is free, so results are offered eagerly, but
each track is only a signed ``/subtitles_proxy`` path: quota is spent when the
viewer picks it, and the converted VTT is cached so it is spent once per file.
"""

import logging
from typing import Dict, List, Optional

import httpx

from core import signing
from core.bounded_cache import BoundedCache
from core.config import get_settings
from core.http_client import http_client

logger = logging.getLogger(__name__)

API_BASE = "https://api.opensubtitles.com/api/v1"
PROXY_PREFIX = "/subtitles_proxy"

# Codes missing here still work and show as the bare code.
LANGUAGE_NAMES = {
    "en": "English", "de": "German", "ja": "Japanese", "es": "Spanish",
    "fr": "French", "it": "Italian", "pt": "Portuguese", "pt-br": "Portuguese (BR)",
    "nl": "Dutch", "pl": "Polish", "ru": "Russian", "ar": "Arabic",
    "zh-cn": "Chinese", "ko": "Korean", "tr": "Turkish", "sv": "Swedish",
    "fi": "Finnish", "da": "Danish", "no": "Norwegian", "cs": "Czech",
    "el": "Greek", "he": "Hebrew", "hu": "Hungarian", "ro": "Romanian",
    "uk": "Ukrainian", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese",
}


class OpenSubtitlesService:
    def __init__(self) -> None:
        self._secret = signing.resolve_secret("SUBTITLES_PROXY_SECRET")
        self._search_cache = BoundedCache(2048)
        # A signed file id maps to immutable content, so a VTT never expires.
        self._vtt_cache = BoundedCache(512)

    def configured(self) -> bool:
        return bool(get_settings().opensubtitles_api_key)

    # The proxy spends quota on whatever file id it is handed, so ids are signed
    # or anyone could drain the quota.
    def _sign(self, file_id: str) -> str:
        return signing.sign(self._secret, file_id)

    def verify(self, file_id: str, sig: str) -> bool:
        return bool(file_id) and signing.verify(self._secret, file_id, sig)

    def _headers(self) -> dict:
        return {
            "Api-Key": get_settings().opensubtitles_api_key,
            "User-Agent": get_settings().opensubtitles_app_name,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def search(
        self,
        tmdb_id: int,
        languages: List[str],
        season: Optional[int] = None,
        episode: Optional[int] = None,
        is_movie: bool = False,
    ) -> List[dict]:
        """``[{url, lang, label}]``, the shape CrimsonPlayer consumes. An upstream
        error is just ``[]``: subtitles are an enhancement, never a failure."""
        if not self.configured():
            return []
        langs = sorted({(l or "").strip().lower() for l in languages if l and l.strip()})
        if not langs:
            return []

        cache_key = f"{tmdb_id}:{season}:{episode}:{is_movie}:{','.join(langs)}"
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return cached

        params = {
            "languages": ",".join(langs),
            # _best_per_language keeps the first track per language, so the most
            # downloaded release wins.
            "order_by": "download_count",
        }
        if is_movie:
            params["tmdb_id"] = str(tmdb_id)
        else:
            # Episodes are keyed by the show's TMDB id plus season and episode.
            params["parent_tmdb_id"] = str(tmdb_id)
            if season is not None:
                params["season_number"] = str(season)
            if episode is not None:
                params["episode_number"] = str(episode)

        try:
            async with http_client() as client:
                resp = await client.get(
                    f"{API_BASE}/subtitles",
                    params=params,
                    headers=self._headers(),
                    timeout=15.0,
                    follow_redirects=True,
                )
        except httpx.RequestError as e:
            logger.warning(f"[opensubtitles] search request failed: {type(e).__name__} - {e}")
            return []

        if resp.status_code != 200:
            logger.warning(f"[opensubtitles] search {resp.status_code} for {cache_key}")
            return []

        try:
            data = resp.json()
        except ValueError:
            logger.warning("[opensubtitles] non-JSON search response")
            return []

        tracks = self._best_per_language(data.get("data") or [], langs)
        self._search_cache.set(cache_key, tracks, ttl=get_settings().subtitles_search_ttl)
        return tracks

    def _best_per_language(self, results: List[dict], langs: List[str]) -> List[dict]:
        """The first track per language, in the viewer's language order."""
        seen: Dict[str, dict] = {}
        for item in results:
            attrs = item.get("attributes") or {}
            lang = (attrs.get("language") or "").lower()
            files = attrs.get("files") or []
            if not lang or not files:
                continue
            file_id = files[0].get("file_id")
            if file_id is None or lang in seen:
                continue
            file_id = str(file_id)
            seen[lang] = {
                "url": f"{PROXY_PREFIX}?f={file_id}&s={self._sign(file_id)}",
                "lang": lang,
                "label": LANGUAGE_NAMES.get(lang, lang.upper()),
            }
        ordered = [seen[l] for l in langs if l in seen]
        ordered += [v for k, v in seen.items() if k not in langs]
        return ordered

    async def fetch_vtt(self, file_id: str) -> Optional[str]:
        """WebVTT text, or None when the download failed (quota exhausted, say)."""
        if not self.configured():
            return None
        cached = self._vtt_cache.get(file_id)
        if cached is not None:
            return cached

        # Minting the temporary link is the step that spends quota.
        try:
            async with http_client() as client:
                dl = await client.post(
                    f"{API_BASE}/download",
                    headers=self._headers(),
                    json={"file_id": int(file_id)},
                    timeout=15.0,
                    follow_redirects=True,
                )
        except (httpx.RequestError, ValueError) as e:
            logger.warning(f"[opensubtitles] download request failed: {type(e).__name__} - {e}")
            return None

        if dl.status_code != 200:
            # 406 means the daily download quota is spent.
            logger.warning(f"[opensubtitles] /download {dl.status_code} for file {file_id}")
            return None

        try:
            link = (dl.json() or {}).get("link")
        except ValueError:
            link = None
        if not link:
            logger.warning(f"[opensubtitles] no link in /download response for {file_id}")
            return None

        try:
            async with http_client() as client:
                sub = await client.get(
                    link,
                    headers={"User-Agent": get_settings().opensubtitles_app_name},
                    timeout=20.0,
                    follow_redirects=True,
                )
        except httpx.RequestError as e:
            logger.warning(f"[opensubtitles] subtitle fetch failed: {type(e).__name__} - {e}")
            return None
        if sub.status_code != 200:
            logger.warning(f"[opensubtitles] subtitle CDN {sub.status_code} for {file_id}")
            return None

        # Files are latin-1 as often as utf-8, so decode leniently.
        vtt = srt_to_vtt(sub.content.decode("utf-8-sig", errors="replace"))
        self._vtt_cache.set(file_id, vtt)
        return vtt


def srt_to_vtt(srt: str) -> str:
    """SRT to the WebVTT a ``<track>`` needs: a ``WEBVTT`` header and ``.`` before
    the milliseconds. Cue indices are dropped so a stray one cannot read as a cue.
    VTT input passes through."""
    text = srt.replace("\r\n", "\n").replace("\r", "\n").strip("\ufeff\n ")
    if text.upper().startswith("WEBVTT"):
        return text + "\n"

    out: List[str] = ["WEBVTT", ""]
    for block in text.split("\n\n"):
        lines = block.split("\n")
        if lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        if "-->" in lines[0]:
            lines[0] = lines[0].replace(",", ".")
        out.append("\n".join(lines))
        out.append("")
    return "\n".join(out)


service = OpenSubtitlesService()
