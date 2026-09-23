"""TMDB art and genres for local titles that only have a filename to go on.

Results are cached per title token, including misses, so a title is looked up
once per cache window. On-disk metadata always wins, except a title the scanner
only had a placeholder for.
"""

import asyncio
import logging
from typing import Dict, List

from core.http_client import http_client
from core.response_cache import local_get, local_set
from metadata_engine.dates import year_from_date
from metadata_engine.tmdb import (
    fetch_tmdb_movie,
    fetch_tmdb_movie_search_results,
    fetch_tmdb_show,
    fetch_tmdb_show_search_results,
)

logger = logging.getLogger("crimson.local.enrichment")

# A title's TMDB match is stable.
_ENRICH_TTL = 6 * 3600
_CONCURRENCY = 8
_FIELDS = ("poster", "genres", "year", "tmdb_id", "backdrop", "description")


def _key(token: str) -> str:
    return f"local-enrich:{token}"


def _is_placeholder_title(item: Dict) -> bool:
    """Empty, or the ``TMDB <id>`` stand-in a ``tmdb-<id>`` folder carries."""
    title = (item.get("title") or "").strip()
    return not title or title.startswith("TMDB ")


def _wants_enrichment(item: Dict) -> bool:
    return _is_placeholder_title(item) or not item.get("poster") or not item.get("genres")


def apply_cached(item: Dict) -> Dict:
    """Overlay whatever enrichment is already cached, with no network call."""
    enrich = local_get(_key(item["id"]))
    if not enrich:
        return item
    out = dict(item)
    if enrich.get("title") and _is_placeholder_title(out):
        out["title"] = enrich["title"]
    for field in _FIELDS:
        if not out.get(field) and enrich.get(field):
            out[field] = enrich[field]
    return out


async def _fetch(client, item: Dict) -> Dict:
    """By id when the item has one, which is exact and includes the title;
    otherwise a title search that supplies art and genres but keeps the item's
    own title."""
    tmdb_id = item.get("tmdb_id")
    is_movie = item.get("media_kind") == "movie"
    if tmdb_id:
        data = await (
            fetch_tmdb_movie(client, tmdb_id) if is_movie else fetch_tmdb_show(client, tmdb_id)
        )
        if not data:
            return {}
        date = data.get("release_date") if is_movie else data.get("first_air_date")
        return {
            "title": data.get("title"),
            "poster": data.get("poster"),
            "backdrop": data.get("backdrop"),
            "genres": data.get("genres") or [],
            "year": item.get("year") or year_from_date(date),
            "description": item.get("description") or data.get("overview"),
            "tmdb_id": tmdb_id,
        }
    title = (item.get("title") or "").strip()
    if not title:
        return {}
    search = fetch_tmdb_movie_search_results if is_movie else fetch_tmdb_show_search_results
    results = await search(client, title, limit=1)
    if not results:
        return {}
    hit = results[0]
    return {
        "poster": hit.get("poster"),
        "genres": hit.get("genres") or [],
        "year": item.get("year") or hit.get("year"),
        "tmdb_id": hit.get("tmdb_id"),
        "backdrop": hit.get("backdrop"),
        "description": item.get("description") or hit.get("overview"),
    }


async def _fetch_and_cache(client, item: Dict) -> None:
    try:
        enrich = await _fetch(client, item)
    except Exception as e:
        logger.debug(f"[local] enrich failed for {item.get('title')!r}: {e}")
        enrich = {}
    local_set(_key(item["id"]), enrich, ttl=_ENRICH_TTL)


async def ensure(item: Dict) -> None:
    """Warm the cache for one title, the overview page's lazy lookup."""
    if local_get(_key(item["id"])) is not None:
        return
    if not _wants_enrichment(item):
        local_set(_key(item["id"]), {}, ttl=_ENRICH_TTL)
        return
    async with http_client() as client:
        await _fetch_and_cache(client, item)


async def warm_id_items(items: List[Dict]) -> None:
    """Warm the titles that carry a tmdb_id, which are exact and cheap. Title
    searches stay lazy on the overview, to avoid a search fan-out per list load."""
    need = [
        it
        for it in items
        if it.get("tmdb_id") and _wants_enrichment(it) and local_get(_key(it["id"])) is None
    ]
    if not need:
        return
    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _one(client, item: Dict) -> None:
        async with sem:
            await _fetch_and_cache(client, item)

    async with http_client() as client:
        await asyncio.gather(*(_one(client, it) for it in need), return_exceptions=True)
