"""
Finds an episode or movie in the operator's Jellyfin library and emits the
``crimson-jellyfin:{itemId}`` marker the JellyfinResolver plays.

Anime libraries hold a show either as one multi-season Series or as one Series
per season, often all tagged with the show's TMDB id and sometimes with
identical season folders. So every candidate Series is collected (by TMDB id and
by title), and the episode is matched by identity first (TMDB episode id, title,
air date, the same data Jellyfin pulls from TMDB), with index-based fallbacks
after that.
"""

import difflib
import logging
import re
from typing import Optional

from core.config import get_settings
from core.http_client import http_client, tmdb_headers
from resolvers.jellyfin import EMBED_MARKER, _ensure_auth, api_get, is_configured

from ._title_match import norm, search_titles
from .base_scraper import BaseAnimeScraper

logger = logging.getLogger(__name__)


def _tmdb_of(item: dict) -> str:
    # Jellyfin's key casing varies ("Tmdb").
    for k, v in (item.get("ProviderIds") or {}).items():
        if k.lower() == "tmdb":
            return str(v)
    return ""


async def _items(uid: str, item_type: str, limit: int, **query) -> list:
    data = await api_get(
        "/Items",
        {"userId": uid, "recursive": "true", "includeItemTypes": item_type, "fields": "ProviderIds", "limit": limit, **query},
    )
    return data.get("Items") or []


async def _tmdb_episode_identity(tmdb_id, season_num: int, episode_num: int) -> Optional[dict]:
    """The target episode's TMDB id, air date and name, which pin it down however
    the library is organised."""
    if not get_settings().tmdb_api_key or tmdb_id is None:
        return None
    try:
        async with http_client() as client:
            r = await client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}",
                headers=tmdb_headers(),
                timeout=15.0,
            )
        if r.status_code != 200:
            return None
        for ep in r.json().get("episodes", []):
            if ep.get("episode_number") == episode_num:
                return {
                    "tmdb_ep_id": str(ep["id"]) if ep.get("id") is not None else None,
                    "air_date": ep.get("air_date") or "",
                    "name": ep.get("name") or "",
                }
    except Exception as e:
        logger.warning("Jellyfin: TMDB episode lookup failed: %s - %s", type(e).__name__, e)
    return None


def _is_generic_name(name: str) -> bool:
    """'Episode 1' and the like, which cannot tell episodes apart."""
    return bool(re.fullmatch(r"(episode|ep\.?|e)\s*\d+", (name or "").strip(), re.I))


def _match_episode(episodes: list, ident: Optional[dict], target_names: list):
    """``(episode, how)`` by TMDB episode id, then exact title, then air date,
    then fuzzy title. Index numbers are useless across per-season Series with
    identical folders; the episode name, from the same TMDB data, is not."""
    norm_targets = [norm(n) for n in target_names if norm(n)]

    ep_id = (ident or {}).get("tmdb_ep_id")
    if ep_id:
        for e in episodes:
            if _tmdb_of(e) == ep_id:
                return e, "tmdb-id"

    if norm_targets:
        for e in episodes:
            if norm(e.get("Name")) in norm_targets:
                return e, "title"

    air = ((ident or {}).get("air_date") or "")[:10]
    if air:
        for e in episodes:
            if (e.get("PremiereDate") or "")[:10] == air:
                return e, "air-date"

    if norm_targets:
        best, best_ratio = None, 0.0
        for e in episodes:
            ne = norm(e.get("Name"))
            if not ne:
                continue
            ratio = max(difflib.SequenceMatcher(None, nt, ne).ratio() for nt in norm_targets)
            if ratio > best_ratio:
                best, best_ratio = e, ratio
        if best is not None and best_ratio >= 0.85:
            return best, f"title-fuzzy({best_ratio:.2f})"

    return None, None


_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8}


def _season_from_name(name: str) -> Optional[int]:
    """The season a Series name indicates ("Season 2", "2nd Season", "Part 2",
    "Show II", "Show 2"), or None for the base series."""
    if not name:
        return None
    n = name.lower()
    for pat in (r"season\s*(\d{1,2})", r"(\d{1,2})(?:st|nd|rd|th)\s+season", r"\b(?:part|cour)\s*(\d{1,2})"):
        m = re.search(pat, n)
        if m:
            return int(m.group(1))
    m = re.search(r"\b(ii|iii|iv|v|vi|vii|viii)\b\s*$", n.strip())
    if m:
        return _ROMAN[m.group(1)]
    m = re.search(r"\s(\d{1,2})\s*$", name.strip())
    # The cap keeps titles such as "86" or "100" from reading as a season.
    if m and int(m.group(1)) <= 12:
        return int(m.group(1))
    return None


def _best_for_season(candidates: list, season_num: int) -> Optional[dict]:
    """The candidate whose name best represents the season, for libraries that
    hold each season as its own Series. A name without a season counts as 1."""
    best, best_score = None, 0
    for c in candidates:
        sn = _season_from_name(c.get("Name") or "")
        if sn == season_num:
            score = 100
        elif sn is None and season_num == 1:
            score = 60
        elif sn is None:
            score = 10
        else:
            score = 0
        if c.get("_tmdb"):
            score += 2
        if score > best_score:
            best, best_score = c, score
    return best if best_score > 0 else None


class JellyfinScraper(BaseAnimeScraper):
    # A movie is an item like an episode, so the same marker plays it.
    SUPPORTS_MOVIES = True

    _media_type: str = "tv"
    _movie_marker: Optional[str] = None
    _candidates: list
    _ep_cache: dict
    _uid: Optional[str] = None
    _tmdb_id = None
    _episodes_list: list

    async def _search_movie(self, media_ctx: dict) -> Optional[str]:
        """The Movie item by TMDB movie id, then by title."""
        if not is_configured():
            return None
        tmdb_id = media_ctx.get("tmdb_id")
        try:
            _token, uid = await _ensure_auth()
        except Exception as e:
            logger.warning("Jellyfin: auth failed: %s - %s", type(e).__name__, e)
            return None

        found: Optional[dict] = None
        try:
            if tmdb_id is not None:
                items = await _items(uid, "Movie", 10, anyProviderIdEquals=f"tmdb.{tmdb_id}")
                found = next((it for it in items if _tmdb_of(it) == str(tmdb_id)), None)

            if not found:
                # The library may have matched the film through another provider.
                titles = search_titles(media_ctx, synonyms=False)[:4]
                norm_targets = {norm(t) for t in titles}
                for term in titles:
                    items = await _items(uid, "Movie", 20, searchTerm=term)
                    found = next((it for it in items if norm(it.get("Name")) in norm_targets), None)
                    if found:
                        break
        except Exception as e:
            logger.warning("Jellyfin: movie lookup failed: %s - %s", type(e).__name__, e)
            return None

        if not found or not found.get("Id"):
            logger.info("Jellyfin: no movie found (tmdb=%s)", tmdb_id)
            return None
        marker = f"{EMBED_MARKER}:{found['Id']}"
        logger.info("Jellyfin: matched movie %r -> %s", found.get("Name"), marker)
        self._movie_marker = marker
        return marker

    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        """Every candidate Series for the show, or the Movie item for a film. The
        returned id only signals success; get_episode_embeds picks per season."""
        self._media_type = media_ctx.get("media_type") or "tv"
        self._movie_marker = None
        if self._media_type == "movie":
            return await self._search_movie(media_ctx)
        self._candidates = []
        self._ep_cache = {}
        self._uid = None
        self._tmdb_id = media_ctx.get("tmdb_id")
        # AniList episode titles back up TMDB's for name matching.
        self._episodes_list = media_ctx.get("episodes_list") or []
        if not is_configured():
            return None

        tmdb_id = self._tmdb_id
        titles = search_titles(media_ctx, synonyms=False)[:4]
        try:
            _token, uid = await _ensure_auth()
            self._uid = uid
        except Exception as e:
            logger.warning("Jellyfin: auth failed: %s - %s", type(e).__name__, e)
            return None

        candidates: dict = {}
        try:
            if tmdb_id is not None:
                try:
                    for it in await _items(uid, "Series", 25, anyProviderIdEquals=f"tmdb.{tmdb_id}"):
                        if _tmdb_of(it) == str(tmdb_id):
                            it["_tmdb"] = True
                            candidates[it.get("Id")] = it
                except Exception:
                    pass

            # Per-season Series are often matched through TVDB and miss the show's
            # TMDB id, so the title search runs even after a TMDB hit.
            for term in titles:
                for it in await _items(uid, "Series", 30, searchTerm=term):
                    candidates.setdefault(it.get("Id"), it)

            if not candidates:
                logger.info("Jellyfin: no series found (tmdb=%s, titles=%r)", tmdb_id, titles)
                return None

            self._candidates = [c for c in candidates.values() if c.get("Id")]
            names = ", ".join(repr(c.get("Name")) for c in self._candidates[:6])
            logger.info("Jellyfin: %d candidate series: %s", len(self._candidates), names)
            return self._candidates[0]["Id"]
        except Exception as e:
            logger.warning("Jellyfin: series lookup failed: %s - %s", type(e).__name__, e)
            return None

    async def _episodes(self, series_id: str) -> list:
        if series_id in self._ep_cache:
            return self._ep_cache[series_id]
        try:
            data = await api_get(
                f"/Shows/{series_id}/Episodes",
                {"userId": self._uid, "fields": "ProviderIds,PremiereDate"},
            )
            eps = data.get("Items") or []
        except Exception as e:
            logger.warning("Jellyfin: episodes fetch failed for %s: %s - %s", series_id, type(e).__name__, e)
            eps = []
        self._ep_cache[series_id] = eps
        return eps

    def _embed(self, ep: dict) -> list[str]:
        item_id = ep.get("Id")
        if not item_id:
            return []
        embed = f"{EMBED_MARKER}:{item_id}"
        logger.info(
            "Jellyfin: matched %r S%sE%s -> %s",
            ep.get("SeriesName"), ep.get("ParentIndexNumber"), ep.get("IndexNumber"), embed,
        )
        return [embed]

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> list[str]:
        if not anime_slug or not is_configured():
            return []
        if self._media_type == "movie":
            return [self._movie_marker] if self._movie_marker else []
        candidates = getattr(self, "_candidates", None) or [{"Id": anime_slug, "Name": ""}]
        if not hasattr(self, "_ep_cache"):
            self._ep_cache = {}

        try:
            all_eps: list = []
            for c in candidates:
                all_eps.extend(await self._episodes(c["Id"]))

            ident = await _tmdb_episode_identity(self._tmdb_id, season_num, episode_num)
            target_names = []
            if ident and ident.get("name"):
                target_names.append(ident["name"])
            for e in getattr(self, "_episodes_list", None) or []:
                if e.get("episode_number") == episode_num and e.get("title"):
                    target_names.append(e["title"])
            target_names = [n for n in target_names if not _is_generic_name(n)]

            match, how = _match_episode(all_eps, ident, target_names)
            if match:
                logger.info(
                    "Jellyfin: S%sE%s matched via %s (target=%r, jellyfin=%r)",
                    season_num, episode_num, how, target_names, match.get("Name"),
                )
                return self._embed(match)

            logger.info(
                "Jellyfin: no identity match for S%sE%s (targets=%r, %d eps across %d series), trying indexes",
                season_num, episode_num, target_names, len(all_eps), len(candidates),
            )

            # A real multi-season Series. Not for season 1: a per-season Series is
            # internally "Season 1" too and would match falsely here.
            if season_num > 1:
                for c in candidates:
                    eps = await self._episodes(c["Id"])
                    match = next(
                        (e for e in eps if e.get("ParentIndexNumber") == season_num and e.get("IndexNumber") == episode_num),
                        None,
                    )
                    if match:
                        return self._embed(match)

            best = _best_for_season(candidates, season_num)
            if best:
                eps = await self._episodes(best["Id"])
                match = next(
                    (e for e in eps if e.get("IndexNumber") == episode_num
                     and e.get("ParentIndexNumber") in (season_num, 1, None)),
                    None,
                ) or next((e for e in eps if e.get("IndexNumber") == episode_num), None)
                if match:
                    return self._embed(match)

            for c in candidates:
                eps = await self._episodes(c["Id"])
                match = next(
                    (e for e in eps if e.get("IndexNumber") == episode_num
                     and e.get("ParentIndexNumber") in (season_num, None)),
                    None,
                )
                if match:
                    return self._embed(match)

            logger.info(
                "Jellyfin: S%sE%s not found across %d candidate series", season_num, episode_num, len(candidates)
            )
            return []
        except Exception as e:
            logger.warning("Jellyfin: episode lookup failed: %s - %s", type(e).__name__, e)
            return []
