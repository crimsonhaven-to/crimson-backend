"""Local library surface: browse / search / play the operator's on-disk media.

The Index gained a second view ("Local") alongside anime; these endpoints feed it
and its search + playback, all keyed by the opaque path token local_engine already
uses (no DB, derived from disk on demand):

  * ``GET /local-library``          — the browsable list of local titles (gzip,
    cached). Returns ``enabled: false`` (not a 404) when no local source is on, so
    the frontend can simply hide the Local view.
  * ``GET /local-overview/{token}`` — one title's detail: metadata + episodes
    (show) or a single play descriptor (movie). Filename-only titles are enriched
    live against TMDB here (best effort, cached).
  * ``GET /search/local``           — filename/title search, shaped like the other
    /search/* surfaces (``suggestions`` tagged ``kind: "local"``).
  * ``GET /watch-local/{token}``     — the /watch NDJSON contract for one local
    file, so the player reuses the exact same source pipeline.
  * ``GET /local_art``               — PUBLIC + signed poster/cover image proxy.

All of these no-op cleanly (empty / 404) unless a local source is enabled.
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.requests import Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.contracts import build_done_line, build_meta_line, build_stream_line
from core.http_client import http_client
from core.response_cache import _local_get, _local_set
from local_engine.fs import is_configured as local_is_configured
from local_engine.library import (
    get_library_item,
    scan_library,
    search_library,
)
from metadata_engine.tmdb import (
    fetch_tmdb_movie_search_results,
    fetch_tmdb_show_search_results,
)
from resolvers.local import LocalResolver

from web.serialization import _gzip_json
from web.util import _ndjson, _public_base_url, _STREAM_HEADERS

logger = logging.getLogger("crimson.local_library")

router = APIRouter()

# In-process cache of the scanned library. Short TTL: the disk can change under
# us, but a scan is the expensive part so we don't want to walk it on every hit.
_ITEMS_KEY = "local-library:v1"
_ITEMS_TTL = 60
# Per-title TMDB enrichment cache (poster/overview/genres borrowed for a
# filename-only title). Longer TTL — a title's TMDB match is stable.
_ENRICH_TTL = 6 * 3600


def _cached_items() -> List[Dict]:
    items = _local_get(_ITEMS_KEY)
    if items is None:
        items = scan_library()
        _local_set(_ITEMS_KEY, items, ttl=_ITEMS_TTL)
    return items


def _enrich_key(token: str) -> str:
    return f"local-enrich:{token}"


def _apply_cached_enrichment(item: Dict) -> Dict:
    """Overlay any cached TMDB enrichment (poster/overview/genres) onto a list item,
    without making a network call. Local artwork + explicit metadata always win."""
    enrich = _local_get(_enrich_key(item["id"]))
    if not enrich:
        return item
    out = dict(item)
    if not out.get("poster") and enrich.get("poster"):
        out["poster"] = enrich["poster"]
    if not out.get("genres") and enrich.get("genres"):
        out["genres"] = enrich["genres"]
    if not out.get("year") and enrich.get("year"):
        out["year"] = enrich["year"]
    if not out.get("tmdb_id") and enrich.get("tmdb_id"):
        out["tmdb_id"] = enrich["tmdb_id"]
    return out


async def _enrich_with_tmdb(item: Dict) -> Dict:
    """Best-effort live TMDB lookup for a filename-only title (no on-disk metadata,
    no tmdb_id, no poster). Borrows the first confident hit's poster/overview/genres
    and caches it. Never raises — enrichment is purely additive."""
    if item.get("has_metadata") and item.get("poster"):
        return item
    if item.get("poster") and item.get("tmdb_id"):
        return item
    title = (item.get("title") or "").strip()
    if not title:
        return item

    cache_k = _enrich_key(item["id"])
    cached = _local_get(cache_k)
    if cached is not None:
        return {**item, **{k: v for k, v in cached.items() if v and not item.get(k)}}

    enrich: Dict = {}
    try:
        async with http_client() as client:
            if item.get("media_kind") == "movie":
                results = await fetch_tmdb_movie_search_results(client, title, limit=1)
            else:
                results = await fetch_tmdb_show_search_results(client, title, limit=1)
        hit = results[0] if results else None
        if hit:
            enrich = {
                "poster": hit.get("poster"),
                "genres": hit.get("genres") or [],
                "year": item.get("year") or hit.get("year"),
                "tmdb_id": hit.get("tmdb_id"),
                "backdrop": hit.get("backdrop"),
                "description": item.get("description") or hit.get("overview"),
            }
    except Exception as e:
        logger.debug(f"[local] tmdb enrich failed for {title!r}: {e}")

    _local_set(cache_k, enrich, ttl=_ENRICH_TTL)
    if not enrich:
        return item
    merged = dict(item)
    for k, v in enrich.items():
        if v and not merged.get(k):
            merged[k] = v
    return merged


def _breakdowns(items: List[Dict]) -> Dict:
    """Kind + genre counts over the whole library, for the Local view's filter chips
    (mirrors /catalogue's categories/genres)."""
    kinds: Dict[str, int] = {}
    genres: Dict[str, int] = {}
    for it in items:
        k = it.get("media_kind") or "show"
        kinds[k] = kinds.get(k, 0) + 1
        for g in it.get("genres") or []:
            genres[g] = genres.get(g, 0) + 1
    return {
        "kinds": [{"kind": k, "count": v} for k, v in sorted(kinds.items())],
        "genres": [{"genre": g, "count": v} for g, v in sorted(genres.items())],
    }


@router.get("/local-library")
async def get_local_library(request: Request):
    """Browsable list of local titles for the Index's "Local" view. Gzip-compressed
    when the client accepts it; ``enabled: false`` when no local source is on."""
    if not local_is_configured():
        return _gzip_json(request, {
            "success": True, "enabled": False,
            "count": 0, "total": 0, "items": [], "kinds": [], "genres": [],
        })
    items = await run_in_threadpool(_cached_items)
    # Overlay any enrichment already computed by prior overview views (no network).
    view = [_apply_cached_enrichment(it) for it in items]
    breakdown = _breakdowns(view)
    return _gzip_json(request, {
        "success": True,
        "enabled": True,
        "count": len(view),
        "total": len(view),
        "items": view,
        "kinds": breakdown["kinds"],
        "genres": breakdown["genres"],
    })


@router.get("/local-overview/{token}")
async def get_local_overview(token: str):
    """One local title's detail: metadata + episodes (show) or a play descriptor
    (movie). 404 when the token doesn't resolve inside a currently enabled root."""
    if not local_is_configured():
        raise HTTPException(status_code=404, detail="Local library not enabled")
    item = await run_in_threadpool(get_library_item, token)
    if not item:
        raise HTTPException(status_code=404, detail="Title not found")
    item = await _enrich_with_tmdb(item)
    return {"success": True, "kind": "local", **item}


@router.get("/search/local")
async def search_local(query_name: str = Query(..., min_length=1, description="Local title/filename to search")):
    """Search the local library by title/filename. Shaped like the other /search/*
    surfaces: ``suggestions`` tagged ``kind: "local"`` so the unified search can
    route a hit to the local overview. Empty (not an error) when disabled."""
    if not local_is_configured():
        return {"success": True, "query": query_name, "count": 0, "suggestions": []}
    items = await run_in_threadpool(_cached_items)
    matches = search_library(query_name, items=items, limit=20)
    suggestions = [
        {
            "id": m["id"],
            "title": m["title"],
            "poster": _apply_cached_enrichment(m).get("poster"),
            "year": m.get("year"),
            "media_kind": m.get("media_kind"),
            "kind": "local",
        }
        for m in matches
    ]
    return {"success": True, "query": query_name, "count": len(suggestions), "suggestions": suggestions}


@router.get("/watch-local/{token}")
async def watch_local(request: Request, token: str, title: Optional[str] = Query(None)):
    """Streaming link(s) for one local file as the same progressive NDJSON the TMDB
    /watch routes emit — so the player reuses the identical source pipeline. Emits a
    ``meta`` line, at most one ``stream`` line (the local file), then ``done``."""
    if not local_is_configured():
        raise HTTPException(status_code=404, detail="Local library not enabled")

    base_url = _public_base_url(request)

    async def _gen():
        yield _ndjson(build_meta_line(
            tmdb_id=0, season_number=None, episode_number=None,
            anilist_id=None, title=title,
        ))
        count = 0
        try:
            resolver = LocalResolver()
            rel = await resolver.resolve(f"crimson-local:{token}")
        except Exception as e:
            logger.warning(f"[watch-local] resolve failed for {token!r}: {e}")
            rel = None
        if rel:
            abs_url = base_url.rstrip("/") + rel if rel.startswith("/") else rel
            stream = {
                "source": "Local",
                "type": "hls" if ".m3u8" in rel.lower() else "mp4",
                "url": abs_url,
            }
            count += 1
            yield _ndjson(build_stream_line(stream))
        yield _ndjson(build_done_line(count))

    return StreamingResponse(_gen(), media_type="application/x-ndjson", headers=_STREAM_HEADERS)
