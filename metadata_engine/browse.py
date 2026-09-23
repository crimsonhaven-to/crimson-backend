"""The browse hubs' catalogues, built from the local tables and cached.

Each catalogue is loaded once per cache window (L1, then L2, then the database),
its genre facets are computed once per load, and the unfiltered response body,
the most requested and identical for everyone, is encoded and gzipped once and
reused. Filtered views are computed per request.
"""

import asyncio
import logging
from collections import Counter
from typing import Callable, Dict, List, Optional

from core import json_response
from core.response_cache import (
    TRENDING_CACHE_TTL,
    get_cached_response,
    local_get,
    local_pop,
    local_set,
    set_cached_response,
)

from . import catalogue
from .tmdb import fetch_trending_anime

logger = logging.getLogger("crimson.browse")

# Bumped from v1 when items gained genres, so pre-genre lists are never served.
ANIME_CACHE_KEY = "catalogue:v2"
LOCAL_ANIME_PER_PAGE = 30


async def _load(items_key: str, builder: Callable[[], List[Dict]]) -> List[Dict]:
    items = local_get(items_key)
    if items is not None:
        return items
    cached = await get_cached_response(items_key)
    if cached and "items" in cached:
        items = cached["items"]
    else:
        items = await asyncio.to_thread(builder)
        if items:
            await set_cached_response(items_key, {"items": items}, ttl_seconds=TRENDING_CACHE_TTL)
    items = items or []
    local_set(items_key, items)
    return items


def genre_facets(items: List[Dict]) -> List[Dict]:
    counts = Counter(g for it in items for g in it.get("genres") or [])
    return [{"genre": g, "count": n} for g, n in sorted(counts.items())]


def _category_facets(items: List[Dict]) -> List[Dict]:
    counts = Counter(it["category"] for it in items)
    return [{"category": c, "count": n} for c, n in sorted(counts.items())]


def _has_genre(item: Dict, genre: str) -> bool:
    wanted = genre.strip().casefold()
    return any((g or "").casefold() == wanted for g in item.get("genres") or [])


async def _catalogue(
    prefix: str,
    builder: Callable[[], List[Dict]],
    list_key: str,
    genre: Optional[str],
    category: Optional[str] = None,
    with_categories: bool = False,
):
    """Either the memoized unfiltered bodies, or a filtered payload dict."""
    derived_key, body_key = f"{prefix}:derived", f"{prefix}:body"
    items = local_get(prefix)
    derived = local_get(derived_key)
    if items is None or derived is None:
        items = await _load(prefix, builder)
        derived = {"genres": genre_facets(items)}
        if with_categories:
            derived["categories"] = _category_facets(items)
        local_set(derived_key, derived)
        local_pop(body_key)

    if not category and not genre:
        bodies = local_get(body_key)
        if bodies is None:
            bodies = json_response.encode(
                {"success": True, "count": len(items), "total": len(items), **derived, list_key: items}
            )
            local_set(body_key, bodies)
        return bodies

    shown = items
    if category:
        wanted = category.strip().upper()
        shown = [it for it in shown if (it["category"] or "").upper() == wanted]
    if genre:
        shown = [it for it in shown if _has_genre(it, genre)]
    return {"success": True, "count": len(shown), "total": len(items), **derived, list_key: shown}


async def anime_catalogue(category: Optional[str], genre: Optional[str]):
    """The full mapped anime archive. The facets describe the whole catalogue so
    every tab and chip renders; ``animes`` honours the filters."""
    return await _catalogue(
        ANIME_CACHE_KEY, catalogue.get_catalogue_items, "animes", genre, category, with_categories=True
    )


async def shows_catalogue(genre: Optional[str]):
    return await _catalogue("catalogue:shows:v1", catalogue.get_shows_catalogue_items, "shows", genre)


async def movies_catalogue(genre: Optional[str]):
    return await _catalogue("catalogue:movies:v1", catalogue.get_movies_catalogue_items, "movies", genre)


# --- local fallback for the anime hub ---------------------------------------------
def _year(item: Dict) -> int:
    year = item.get("year")
    return int(year) if str(year or "").isdigit() else 0


def order_local_anime(items: List[Dict], sort: str) -> List[Dict]:
    """There is no local score, so every non-title sort puts poster-bearing,
    newest titles first as a trending stand-in."""
    if sort == "title":
        return sorted(items, key=lambda it: (it.get("title") or "").lower())
    if sort == "newest":
        return sorted(items, key=lambda it: (-_year(it), (it.get("title") or "").lower()))
    return sorted(items, key=lambda it: (0 if it.get("poster") else 1, -_year(it), (it.get("title") or "").lower()))


async def local_anime_fallback(client, *, genre, sort, page, per_page=LOCAL_ANIME_PER_PAGE) -> Dict:
    """One page of the anime hub from the local archive, for when AniList's browse
    API is down. The default view is topped with TMDB trending, which also lends
    posters to posterless local twins; if TMDB is down too, the top-up is skipped."""
    items = await _load(ANIME_CACHE_KEY, catalogue.get_catalogue_items)
    cards = [dict(it, kind="anime") for it in items if it.get("anilist_id")]
    genres = genre_facets(cards)
    if genre:
        cards = [it for it in cards if _has_genre(it, genre)]
    ordered = order_local_anime(cards, sort)

    if not genre and sort in ("trending", "popular"):
        try:
            trending = await fetch_trending_anime(client, limit=20)
        except Exception as e:
            logger.warning(f"Anime fallback TMDB top-up failed: {e}")
            trending = []
        by_id = {it["anilist_id"]: it for it in ordered}
        seen: set = set()
        head = []
        for t in trending:
            aid = t.get("anilist_id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            local = by_id.get(aid)
            if local:
                head.append({**local, "poster": local.get("poster") or t.get("poster")})
            else:
                head.append({
                    "anilist_id": aid, "kind": "anime", "title": t.get("title"),
                    "poster": t.get("poster"), "year": t.get("year"), "genres": [],
                })
        ordered = head + [it for it in ordered if it["anilist_id"] not in seen]

    start = (page - 1) * per_page
    return {
        "items": ordered[start:start + per_page],
        "total": len(ordered),
        "page": page,
        "has_next": start + per_page < len(ordered),
        "genres": genres,
    }
