"""
OpenSubtitles service — search + download + SRT→VTT conversion for the player's
external subtitle tracks.

Why this is server-side (vs. movie-web's browser-side approach): OpenSubtitles
requires an ``Api-Key`` header, and its ``/download`` endpoint is **quota-limited
per key** (the free tier is small). Putting the key in the frontend bundle would
expose it and let any visitor burn the shared quota. So the key stays here, and —
crucially — we *cache* both searches and converted VTTs so the same episode never
spends quota twice. The browser only ever sees a signed, same-origin
``/subtitles_proxy`` path; the OpenSubtitles key never leaves the backend.

Two-stage flow, mirroring how the resolvers split "find" from "fetch":

  1. ``search()`` hits ``GET /api/v1/subtitles`` (keyed off the TMDB id — the whole
     site is TMDB-keyed). This is **free** (no download quota), so we can offer the
     list eagerly. Each result is returned as a signed ``/subtitles_proxy?f=…``
     path, NOT a raw link — we don't spend a download until the viewer actually
     picks that language.
  2. ``fetch_vtt(file_id)`` (called by the proxy route) hits ``POST /api/v1/download``
     to mint a temporary CDN link for that ``file_id`` — *this* is what costs quota
     — fetches the SRT and converts it to WebVTT. The result is cached by
     ``file_id`` so re-selecting a track / seeking / a second viewer is free.

Config (all optional — unset disables the feature, GET /subtitles -> 503):
  * ``OPENSUBTITLES_API_KEY``  — required to enable. Get one at
    https://www.opensubtitles.com/en/consumers (register an app).
  * ``OPENSUBTITLES_APP_NAME`` — the OpenSubtitles API *requires* a descriptive
    ``User-Agent`` identifying the app; defaults to ``CrimsonHaven v1.0``.
  * ``PROXY_SECRET`` — reused to HMAC-sign the ``/subtitles_proxy`` file ids so the
    proxy can't be driven to spend our download quota on arbitrary file ids.
  * ``SUBTITLES_SEARCH_TTL`` — seconds to cache a search result (default 3600).
"""

import hashlib
import hmac
import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import httpx

from resolvers._proxy_secret import resolve_secret as _resolve_proxy_secret

logger = logging.getLogger(__name__)

API_BASE = "https://api.opensubtitles.com/api/v1"
PROXY_PREFIX = "/subtitles_proxy"

# Human-friendly names for the language codes we surface in the UI. Anything the
# API returns that isn't here still works — it just shows the bare code.
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
    """Thin, quota-aware OpenSubtitles client. One instance is shared process-wide
    (see ``subtitles_engine.service.service``); it holds the small in-memory caches
    so quota is spent at most once per (search) / per (file_id)."""

    def __init__(self) -> None:
        self._api_key = (os.getenv("OPENSUBTITLES_API_KEY") or "").strip()
        self._app_name = (os.getenv("OPENSUBTITLES_APP_NAME") or "CrimsonHaven v1.0").strip()
        try:
            self._search_ttl = float(os.getenv("SUBTITLES_SEARCH_TTL") or 3600)
        except ValueError:
            self._search_ttl = 3600.0
        self._secret = _resolve_proxy_secret("SUBTITLES_PROXY_SECRET")

        # search cache: key -> (expires_at, list[track dict])
        self._search_cache: Dict[str, Tuple[float, List[dict]]] = {}
        # converted-VTT cache: file_id -> (vtt_text, downloaded_at). Unbounded in
        # principle but VTTs are tiny (tens of KB) and key space is small in
        # practice; trimmed lazily in _cache_vtt.
        self._vtt_cache: Dict[str, Tuple[str, float]] = {}
        self._VTT_CACHE_MAX = 512

    def configured(self) -> bool:
        return bool(self._api_key)

    # -- signing ------------------------------------------------------------
    # The proxy mints a fresh download (spending quota) for whatever file_id it's
    # handed, so an unsigned proxy would let anyone drain our OpenSubtitles quota.
    # Every file_id we hand out is HMAC-signed; the proxy refuses unsigned ids.
    def _sign(self, file_id: str) -> str:
        return hmac.new(self._secret, file_id.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

    def verify(self, file_id: str, sig: str) -> bool:
        return bool(file_id) and hmac.compare_digest(self._sign(file_id), sig or "")

    def _headers(self) -> dict:
        return {
            "Api-Key": self._api_key,
            "User-Agent": self._app_name,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -- search -------------------------------------------------------------
    async def search(
        self,
        tmdb_id: int,
        languages: List[str],
        season: Optional[int] = None,
        episode: Optional[int] = None,
        is_movie: bool = False,
    ) -> List[dict]:
        """Return external subtitle tracks for a title as
        ``[{url, lang, label}]`` — the exact shape CrimsonPlayer consumes.

        ``url`` is a signed same-origin ``/subtitles_proxy`` path, so the file is
        only downloaded (quota) when the viewer selects it. Cached for
        ``SUBTITLES_SEARCH_TTL``; an upstream error returns ``[]`` (subtitles are a
        best-effort enhancement, never a hard failure)."""
        if not self.configured():
            return []
        langs = sorted({(l or "").strip().lower() for l in languages if l and l.strip()})
        if not langs:
            return []

        cache_key = f"{tmdb_id}:{season}:{episode}:{is_movie}:{','.join(langs)}"
        cached = self._search_cache.get(cache_key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]

        params = {
            "languages": ",".join(langs),
            # Prefer non-hearing-impaired, machine-readable srt; OS still returns
            # others but ordering nudges the best release first.
            "order_by": "download_count",
        }
        if is_movie:
            params["tmdb_id"] = str(tmdb_id)
        else:
            # For an episode OpenSubtitles keys on the SHOW's tmdb id plus S/E.
            params["parent_tmdb_id"] = str(tmdb_id)
            if season is not None:
                params["season_number"] = str(season)
            if episode is not None:
                params["episode_number"] = str(episode)

        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                resp = await client.get(
                    f"{API_BASE}/subtitles", params=params, headers=self._headers()
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
        self._search_cache[cache_key] = (now + self._search_ttl, tracks)
        return tracks

    def _best_per_language(self, results: List[dict], langs: List[str]) -> List[dict]:
        """Collapse the raw OpenSubtitles results to the single best track per
        language (results are already ordered by download_count), in the order the
        viewer asked for. Each becomes a signed proxy URL."""
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
        # Preserve the caller's language priority order; append any extras.
        ordered = [seen[l] for l in langs if l in seen]
        ordered += [v for k, v in seen.items() if k not in langs]
        return ordered

    # -- download + convert -------------------------------------------------
    async def fetch_vtt(self, file_id: str) -> Optional[str]:
        """Resolve a ``file_id`` to WebVTT text, spending one OpenSubtitles
        download (quota) the first time and caching the result thereafter.

        Returns the VTT string, or ``None`` if the download/convert failed (e.g.
        quota exhausted) — the proxy route turns that into a 502/404."""
        if not self.configured():
            return None
        cached = self._vtt_cache.get(file_id)
        if cached:
            return cached[0]

        # 1. Mint a temporary download link (THIS spends quota).
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                dl = await client.post(
                    f"{API_BASE}/download",
                    headers=self._headers(),
                    json={"file_id": int(file_id)},
                )
        except (httpx.RequestError, ValueError) as e:
            logger.warning(f"[opensubtitles] download request failed: {type(e).__name__} - {e}")
            return None

        if dl.status_code != 200:
            # 406 = daily download quota exhausted; logged so it's visible.
            logger.warning(f"[opensubtitles] /download {dl.status_code} for file {file_id}")
            return None

        try:
            link = (dl.json() or {}).get("link")
        except ValueError:
            link = None
        if not link:
            logger.warning(f"[opensubtitles] no link in /download response for {file_id}")
            return None

        # 2. Fetch the actual subtitle file from the CDN link.
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                sub = await client.get(link, headers={"User-Agent": self._app_name})
        except httpx.RequestError as e:
            logger.warning(f"[opensubtitles] subtitle fetch failed: {type(e).__name__} - {e}")
            return None
        if sub.status_code != 200:
            logger.warning(f"[opensubtitles] subtitle CDN {sub.status_code} for {file_id}")
            return None

        # OpenSubtitles files are usually latin-1/utf-8 SRT; decode leniently.
        raw = sub.content.decode("utf-8-sig", errors="replace")
        vtt = srt_to_vtt(raw)
        self._cache_vtt(file_id, vtt)
        return vtt

    def _cache_vtt(self, file_id: str, vtt: str) -> None:
        if len(self._vtt_cache) >= self._VTT_CACHE_MAX:
            # Drop the oldest ~10% to bound memory (VTTs are tiny, churn is rare).
            for k in sorted(self._vtt_cache, key=lambda k: self._vtt_cache[k][1])[: self._VTT_CACHE_MAX // 10]:
                self._vtt_cache.pop(k, None)
        self._vtt_cache[file_id] = (vtt, time.monotonic())


# --- SRT -> WebVTT ---------------------------------------------------------
def srt_to_vtt(srt: str) -> str:
    """Convert SubRip (SRT) text to WebVTT, which is what HTML5 ``<track>`` wants.

    The two formats are nearly identical; the differences that matter for the
    browser are: VTT needs a ``WEBVTT`` header, and its timestamps use a ``.``
    (not ``,``) before the milliseconds. We also strip the numeric cue indices
    (optional in VTT) so a stray index can't be mistaken for a cue. Already-VTT
    input is returned essentially unchanged."""
    text = srt.replace("\r\n", "\n").replace("\r", "\n").strip("﻿\n ")
    if text.upper().startswith("WEBVTT"):
        return text + "\n"

    out: List[str] = ["WEBVTT", ""]
    for block in text.split("\n\n"):
        lines = block.split("\n")
        if not lines:
            continue
        # Drop a leading numeric cue index (SRT-only).
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue
        # Timing line: replace the comma decimal separator with a dot.
        if "-->" in lines[0]:
            lines[0] = lines[0].replace(",", ".")
        out.append("\n".join(lines))
        out.append("")
    return "\n".join(out)


# Shared, process-wide instance (caches live here).
service = OpenSubtitlesService()
