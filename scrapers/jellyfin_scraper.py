import difflib
import os
import re
from typing import Optional

import httpx

from .base_scraper import BaseAnimeScraper
# The Jellyfin client (auth + API + config) lives with the resolver, which owns
# the heavy lifting — the scraper just locates the episode item.
from resolvers.jellyfin import EMBED_MARKER, api_get, is_configured, _ensure_auth


def _tmdb_of(item: dict) -> str:
    pids = item.get("ProviderIds") or {}
    # Jellyfin casing varies ("Tmdb"); match case-insensitively.
    for k, v in pids.items():
        if k.lower() == "tmdb":
            return str(v)
    return ""


async def _tmdb_episode_identity(tmdb_id, season_num: int, episode_num: int) -> Optional[dict]:
    """TMDB identity of a target episode: its TMDB episode id + air date.

    This is the reliable, structure-agnostic key for matching the Jellyfin
    episode — Jellyfin pulls the same data from TMDB, so even when season
    folders are named identically, the episode's TMDB id / air date pin down the
    exact episode regardless of how the library is organised.
    """
    key = os.getenv("TMDB_API_KEY")
    if not key or tmdb_id is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}",
                headers={"Authorization": f"Bearer {key}", "accept": "application/json"},
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
        print(f"[JellyfinScraper] TMDB episode lookup failed: {type(e).__name__} - {e}")
    return None


def _norm(s: str) -> str:
    """Normalise an episode title for comparison (lowercase, alnum only)."""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _is_generic_name(name: str) -> bool:
    """True for placeholder titles like 'Episode 1' that can't disambiguate."""
    return bool(re.fullmatch(r"(episode|ep\.?|e)\s*\d+", (name or "").strip(), re.I))


def _match_episode(episodes: list, ident: Optional[dict], target_names: list):
    """Find the Jellyfin episode for a target, returning ``(episode, how)``.

    Priority: exact TMDB episode id -> exact episode title -> air date ->
    fuzzy episode title. Title matching is the key signal here — index/season
    numbers are unreliable across per-season-split, identically named folders,
    but the episode *name* (from TMDB, same source Jellyfin uses) is not.
    """
    norm_targets = [_norm(n) for n in target_names if _norm(n)]

    # a) Exact TMDB episode id (when Jellyfin stored episode-level provider ids).
    ep_id = (ident or {}).get("tmdb_ep_id")
    if ep_id:
        for e in episodes:
            if _tmdb_of(e) == ep_id:
                return e, "tmdb-id"

    # b) Exact episode title.
    if norm_targets:
        for e in episodes:
            if _norm(e.get("Name")) in norm_targets:
                return e, "title"

    # c) Air date — globally unique per episode in good TMDB metadata.
    air = ((ident or {}).get("air_date") or "")[:10]
    if air:
        for e in episodes:
            if (e.get("PremiereDate") or "")[:10] == air:
                return e, "air-date"

    # d) Fuzzy episode title (handles minor punctuation / romanisation drift).
    if norm_targets:
        best, best_ratio = None, 0.0
        for e in episodes:
            ne = _norm(e.get("Name"))
            if not ne:
                continue
            ratio = max(difflib.SequenceMatcher(None, nt, ne).ratio() for nt in norm_targets)
            if ratio > best_ratio:
                best, best_ratio = e, ratio
        if best is not None and best_ratio >= 0.85:
            return best, f"title-fuzzy({best_ratio:.2f})"

    return None, None


_ROMAN = {"ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8}

# Trailing season indicators stripped from a search title so a per-season AniList
# title ("Show Season 2", "Show 2nd Season", "Show II", "Show Part 2") still finds
# a base-named Jellyfin Series ("Show"). Mirrors _season_from_name's vocabulary.
_SEASON_SUFFIX_PATTERNS = [
    r"\s*[:\-]?\s*season\s*\d{1,2}\s*$",
    r"\s*\d{1,2}(?:st|nd|rd|th)\s+season\s*$",
    r"\s*[:\-]?\s*(?:part|cour)\s*\d{1,2}\s*$",
    r"\s+(?:ii|iii|iv|v|vi|vii|viii)\s*$",
]


def _strip_season_suffix(title: str) -> Optional[str]:
    """Return ``title`` with a trailing season indicator removed, or None if it
    carries none (so callers can skip a redundant duplicate search). Bare
    trailing numbers are intentionally NOT stripped — too many titles legitimately
    end in one (e.g. "86")."""
    if not title:
        return None
    t = title.strip()
    for pat in _SEASON_SUFFIX_PATTERNS:
        stripped = re.sub(pat, "", t, flags=re.I).strip()
        if stripped and stripped != t:
            return stripped
    return None


def _search_titles(media_ctx: dict) -> list[str]:
    """Ordered, deduped list of titles to search Jellyfin by.

    Includes the primary/english/romaji titles AND their season-suffix-stripped
    forms, so a season-specific title ("... Season 2") still discovers the base
    Jellyfin series in a multi-season library. Capped to keep request count sane.
    """
    titles: list[str] = []
    for key in ("title", "title_english", "title_romaji"):
        v = media_ctx.get(key)
        if v:
            titles.append(v)
            base = _strip_season_suffix(v)
            if base:
                titles.append(base)
    # Dedupe (case-insensitively) while preserving order.
    seen, ordered = set(), []
    for t in titles:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            ordered.append(t)
    return ordered[:4]


def _season_from_name(name: str) -> Optional[int]:
    """Best-effort season number parsed from a Jellyfin series name.

    Handles "Season 2", "2nd Season", "Part 2", "Cour 2", trailing roman
    numerals ("Show II") and a small trailing number ("Show 2"). Returns None
    when the name carries no season indicator (i.e. the base/first series).
    """
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
    if m and int(m.group(1)) <= 12:  # cap avoids matching titles like "86" / "100"
        return int(m.group(1))
    return None


def _best_for_season(candidates: list, season_num: int) -> Optional[dict]:
    """Pick the candidate series whose NAME best represents the target season.

    Used for per-season-split libraries (each season is its own Jellyfin series).
    A name with no season indicator is treated as season 1.
    """
    best, best_score = None, 0
    for c in candidates:
        sn = _season_from_name(c.get("Name") or "")
        if sn == season_num:
            score = 100
        elif sn is None and season_num == 1:
            score = 60
        elif sn is None:
            score = 10  # unknown — weak fallback
        else:
            score = 0  # name clearly indicates a *different* season
        if c.get("_tmdb"):
            score += 2  # prefer a TMDB-id match on a tie
        if score > best_score:
            best, best_score = c, score
    return best if best_score > 0 else None


class JellyfinScraper(BaseAnimeScraper):
    """
    Jellyfin scraper — locates an episode in the user's own Jellyfin library.

    Disabled (returns nothing) unless JELLYFIN_URL/USERNAME are configured.

    Anime libraries organise a show two different ways, and this handles both:
      * one multi-season Series (Season 1/2/… as children), or
      * one Series *per season* (the common anime layout), all sharing the
        show's TMDB id.
    So ``search_anime`` collects every candidate Series (by TMDB id AND title),
    and ``get_episode_embeds`` then locates the episode by structure first (a
    real ``ParentIndexNumber == season``), falling back to name-matching the
    right per-season Series. The emitted embed URL is the
    ``crimson-jellyfin:{episodeItemId}`` marker the resolver turns into a
    proxied, ad-free stream. See [[jellyfin-source]].
    """

    # Jellyfin holds movies as Type "Movie" items, keyed by their TMDB *movie* id;
    # the resolver's crimson-jellyfin:{itemId} marker is item-based, so a movie
    # resolves exactly like an episode once we locate its item.
    SUPPORTS_MOVIES = True

    async def _search_movie(self, media_ctx: dict) -> Optional[str]:
        """Locate a Movie item in the library by TMDB *movie* id (then title) and
        return its crimson-jellyfin marker. media_ctx carries the movie title."""
        if not is_configured():
            return None
        tmdb_id = media_ctx.get("tmdb_id")
        try:
            _token, uid = await _ensure_auth()
        except Exception as e:
            print(f"[JellyfinScraper] Auth failed: {type(e).__name__} - {e}")
            return None

        found: Optional[dict] = None
        try:
            if tmdb_id is not None:
                data = await api_get(
                    "/Items",
                    {
                        "userId": uid,
                        "recursive": "true",
                        "includeItemTypes": "Movie",
                        "anyProviderIdEquals": f"tmdb.{tmdb_id}",
                        "fields": "ProviderIds",
                        "limit": 10,
                    },
                )
                found = next(
                    (it for it in (data.get("Items") or []) if _tmdb_of(it) == str(tmdb_id)),
                    None,
                )

            if not found:
                # Fall back to a title match (library may be tagged via another id).
                norm_targets = {_norm(t) for t in _search_titles(media_ctx)}
                norm_targets.discard("")
                for term in _search_titles(media_ctx):
                    data = await api_get(
                        "/Items",
                        {
                            "userId": uid,
                            "recursive": "true",
                            "includeItemTypes": "Movie",
                            "searchTerm": term,
                            "fields": "ProviderIds",
                            "limit": 20,
                        },
                    )
                    found = next(
                        (it for it in (data.get("Items") or []) if _norm(it.get("Name")) in norm_targets),
                        None,
                    )
                    if found:
                        break
        except Exception as e:
            print(f"[JellyfinScraper] Movie lookup failed: {type(e).__name__} - {e}")
            return None

        if not found or not found.get("Id"):
            print(f"[JellyfinScraper] No movie found (tmdb={tmdb_id}).")
            return None
        marker = f"{EMBED_MARKER}:{found['Id']}"
        print(f"[JellyfinScraper] Matched movie {found.get('Name')!r} -> {marker}")
        self._movie_marker = marker
        return marker

    async def search_anime(self, media_ctx: dict) -> Optional[str]:
        """Collect candidate Jellyfin Series for this show (TMDB id + title), or
        locate a single Movie item when this is a movie request."""
        self._media_type = media_ctx.get("media_type") or "tv"
        self._movie_marker: Optional[str] = None
        if self._media_type == "movie":
            return await self._search_movie(media_ctx)
        self._candidates: list = []
        self._ep_cache: dict = {}
        self._uid: Optional[str] = None
        self._tmdb_id = media_ctx.get("tmdb_id")
        # Backup episode-title source (AniList) for name matching, in case TMDB
        # is unavailable: episodes_list = [{episode_number, title, ...}, ...].
        self._episodes_list = media_ctx.get("episodes_list") or []
        if not is_configured():
            return None

        tmdb_id = media_ctx.get("tmdb_id")
        search_titles = _search_titles(media_ctx)

        try:
            _token, uid = await _ensure_auth()
            self._uid = uid
        except Exception as e:
            print(f"[JellyfinScraper] Auth failed: {type(e).__name__} - {e}")
            return None

        candidates: dict = {}  # keyed by item Id, deduped across both searches
        try:
            # 1) By TMDB id (reliable). Tag matches so they win ties later.
            if tmdb_id is not None:
                try:
                    data = await api_get(
                        "/Items",
                        {
                            "userId": uid,
                            "recursive": "true",
                            "includeItemTypes": "Series",
                            "anyProviderIdEquals": f"tmdb.{tmdb_id}",
                            "fields": "ProviderIds",
                            "limit": 25,
                        },
                    )
                    for it in data.get("Items") or []:
                        if _tmdb_of(it) == str(tmdb_id):
                            it["_tmdb"] = True
                            candidates[it.get("Id")] = it
                except Exception:
                    pass

            # 2) By title — catches per-season Series Jellyfin didn't tag with
            #    the show's TMDB id (e.g. matched via TVDB). Searches several
            #    title variants (incl. season-suffix-stripped) so a per-season
            #    title like "... Season 2" still finds the base-named series.
            for term in search_titles:
                data = await api_get(
                    "/Items",
                    {
                        "userId": uid,
                        "recursive": "true",
                        "includeItemTypes": "Series",
                        "searchTerm": term,
                        "fields": "ProviderIds",
                        "limit": 30,
                    },
                )
                for it in data.get("Items") or []:
                    candidates.setdefault(it.get("Id"), it)

            if not candidates:
                print(f"[JellyfinScraper] No series found (tmdb={tmdb_id}, titles={search_titles!r}).")
                return None

            self._candidates = [c for c in candidates.values() if c.get("Id")]
            names = ", ".join(repr(c.get("Name")) for c in self._candidates[:6])
            print(f"[JellyfinScraper] {len(self._candidates)} candidate series: {names}")
            # Any truthy slug signals success; the real selection happens per
            # season in get_episode_embeds using self._candidates.
            return self._candidates[0]["Id"]
        except Exception as e:
            print(f"[JellyfinScraper] Series lookup failed: {type(e).__name__} - {e}")
            return None

    async def _episodes(self, series_id: str) -> list:
        """Fetch (and cache) all episodes of a Series."""
        if series_id in self._ep_cache:
            return self._ep_cache[series_id]
        try:
            data = await api_get(
                f"/Shows/{series_id}/Episodes",
                {"userId": self._uid, "fields": "ProviderIds,PremiereDate"},
            )
            eps = data.get("Items") or []
        except Exception as e:
            print(f"[JellyfinScraper] Episodes fetch failed for {series_id}: {type(e).__name__} - {e}")
            eps = []
        self._ep_cache[series_id] = eps
        return eps

    def _embed(self, ep: dict) -> list[str]:
        item_id = ep.get("Id")
        if not item_id:
            return []
        embed = f"{EMBED_MARKER}:{item_id}"
        print(
            f"[JellyfinScraper] Matched {ep.get('SeriesName')!r} "
            f"S{ep.get('ParentIndexNumber')}E{ep.get('IndexNumber')} -> {embed}"
        )
        return [embed]

    async def get_episode_embeds(
        self, anime_slug: str, episode_num: int, season_num: int = 1
    ) -> list[str]:
        """Locate the target episode across the show's candidate series (or return
        the already-located movie marker for a movie request)."""
        if not anime_slug or not is_configured():
            return []
        if getattr(self, "_media_type", "tv") == "movie":
            marker = getattr(self, "_movie_marker", None)
            return [marker] if marker else []
        candidates = getattr(self, "_candidates", None) or [{"Id": anime_slug, "Name": ""}]

        try:
            # Strategy 0 (primary) — match by episode IDENTITY, not by index.
            # Pool every candidate series' episodes, then match on the episode's
            # TMDB id / TITLE / air date. Title matching is the workhorse: it
            # resolves the right episode even across identically named, per-season
            # split folders where season/index numbers are useless.
            all_eps: list = []
            for c in candidates:
                all_eps.extend(await self._episodes(c["Id"]))

            ident = await _tmdb_episode_identity(getattr(self, "_tmdb_id", None), season_num, episode_num)

            # Target episode titles: TMDB name first (same source Jellyfin uses),
            # then the AniList title as a backup.
            target_names = []
            if ident and ident.get("name"):
                target_names.append(ident["name"])
            for e in getattr(self, "_episodes_list", []) or []:
                if e.get("episode_number") == episode_num and e.get("title"):
                    target_names.append(e["title"])
            target_names = [n for n in target_names if not _is_generic_name(n)]

            match, how = _match_episode(all_eps, ident, target_names)
            if match:
                print(f"[JellyfinScraper] S{season_num}E{episode_num} matched via {how} "
                      f"(target={target_names!r}, jellyfin={match.get('Name')!r})")
                return self._embed(match)

            print(f"[JellyfinScraper] No identity match for S{season_num}E{episode_num} "
                  f"(target_names={target_names!r}, pooled {len(all_eps)} eps from "
                  f"{len(candidates)} series); falling back to index matching.")

            # Strategy 1 — a genuine multi-season Series that really has this
            # season (ParentIndexNumber == season_num). Trustworthy for season
            # > 1; for season 1 we prefer the name-based pick below, since a
            # per-season Series is internally "Season 1" too and would falsely
            # match here.
            if season_num > 1:
                for c in candidates:
                    eps = await self._episodes(c["Id"])
                    match = next(
                        (e for e in eps if e.get("ParentIndexNumber") == season_num and e.get("IndexNumber") == episode_num),
                        None,
                    )
                    if match:
                        return self._embed(match)

            # Strategy 2 — per-season-split (or season 1): pick the series whose
            # name represents this season, then match the episode by its index.
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

            # Strategy 3 — last resort: any candidate with a matching episode
            # index in the requested season.
            for c in candidates:
                eps = await self._episodes(c["Id"])
                match = next(
                    (e for e in eps if e.get("IndexNumber") == episode_num
                     and e.get("ParentIndexNumber") in (season_num, None)),
                    None,
                )
                if match:
                    return self._embed(match)

            print(
                f"[JellyfinScraper] Episode S{season_num}E{episode_num} not found "
                f"across {len(candidates)} candidate series."
            )
            return []
        except Exception as e:
            print(f"[JellyfinScraper] Episode lookup failed: {type(e).__name__} - {e}")
            return []
