"""The local library's browse, search and play routes, keyed by the opaque path
token the scanner derives from disk. Everything answers empty or 404 while no
local source is enabled, so the client can simply hide the view."""

import asyncio
import logging
from collections import Counter
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core import json_response
from core.contracts import build_done_line, build_meta_line, build_stream_line
from core.public_url import public_base_url
from core.response_cache import local_get, local_set
from playback_engine import ndjson
from resolvers.local import LocalResolver

from . import enrichment
from .fs import EMBED_MARKER, is_configured
from .library import browse_dir, get_library_item, scan_library, search_library

logger = logging.getLogger("crimson.local_library")

router = APIRouter(tags=["local"])

# The disk can change underneath, but the scan is the expensive part.
_ITEMS_KEY = "local-library:v1"
_ITEMS_TTL = 60


def _cached_items() -> List[Dict]:
    items = local_get(_ITEMS_KEY)
    if items is None:
        items = scan_library()
        local_set(_ITEMS_KEY, items, ttl=_ITEMS_TTL)
    return items


def _require_enabled() -> None:
    if not is_configured():
        raise HTTPException(status_code=404, detail="Local library not enabled")


def _facets(items: List[Dict]) -> Dict:
    kinds = Counter(it.get("media_kind") or "show" for it in items)
    genres = Counter(g for it in items for g in it.get("genres") or [])
    return {
        "kinds": [{"kind": k, "count": n} for k, n in sorted(kinds.items())],
        "genres": [{"genre": g, "count": n} for g, n in sorted(genres.items())],
    }


@router.get("/local-library")
async def get_local_library(request: Request):
    if not is_configured():
        return json_response.gzip_json(
            request,
            {
                "success": True,
                "enabled": False,
                "count": 0,
                "total": 0,
                "items": [],
                "kinds": [],
                "genres": [],
            },
        )
    items = await asyncio.to_thread(_cached_items)
    await enrichment.warm_id_items(items)
    view = [enrichment.apply_cached(it) for it in items]
    return json_response.gzip_json(
        request,
        {
            "success": True,
            "enabled": True,
            "count": len(view),
            "total": len(view),
            "items": view,
            **_facets(view),
        },
    )


@router.get("/local-overview/{token}")
async def get_local_overview(token: str):
    """Metadata and episodes, or a play descriptor for a movie."""
    _require_enabled()
    item = await asyncio.to_thread(get_library_item, token)
    if not item:
        raise HTTPException(status_code=404, detail="Title not found")
    await enrichment.ensure(item)
    return {"success": True, "kind": "local", **enrichment.apply_cached(item)}


@router.get("/local-browse")
async def get_local_browse(
    token: Optional[str] = Query(
        None, description="Directory token; omit for the source-root level"
    ),
):
    """The children of one directory, or the enabled roots. ``title`` entries get
    the cached art; ``folder`` and ``file`` entries are the raw fallback for media
    that never resolved to a title."""
    _require_enabled()
    view = await asyncio.to_thread(browse_dir, token)
    if view is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    view["entries"] = [
        {**enrichment.apply_cached(e), "type": "title"} if e.get("type") == "title" else e
        for e in view.get("entries", [])
    ]
    return {"success": True, "kind": "local-browse", **view}


@router.get("/search/local")
async def search_local(
    query_name: str = Query(..., min_length=1, description="Local title/filename to search"),
):
    """Shaped like the other /search/* surfaces. Matches the enriched titles, so a
    ``tmdb-<id>`` folder is findable by its real name once its art is cached."""
    if not is_configured():
        return {"success": True, "query": query_name, "count": 0, "suggestions": []}
    items = await asyncio.to_thread(_cached_items)
    matches = search_library(
        query_name, items=[enrichment.apply_cached(it) for it in items], limit=20
    )
    suggestions = [
        {
            "id": m["id"],
            "title": m["title"],
            "poster": m.get("poster"),
            "year": m.get("year"),
            "media_kind": m.get("media_kind"),
            "kind": "local",
        }
        for m in matches
    ]
    return {
        "success": True,
        "query": query_name,
        "count": len(suggestions),
        "suggestions": suggestions,
    }


@router.get("/watch-local/{token}")
async def watch_local(request: Request, token: str, title: Optional[str] = Query(None)):
    """The /watch NDJSON contract for one file, so the player needs no second
    pipeline: ``meta``, at most one ``stream``, then ``done``."""
    _require_enabled()
    base_url = public_base_url(request)

    async def _lines():
        yield ndjson.line(
            build_meta_line(
                tmdb_id=0,
                season_number=None,
                episode_number=None,
                anilist_id=None,
                title=title,
            )
        )
        try:
            rel = await LocalResolver().resolve(f"{EMBED_MARKER}:{token}")
        except Exception as e:
            logger.warning(f"[watch-local] resolve failed for {token!r}: {e}")
            rel = None
        if rel:
            yield ndjson.line(
                build_stream_line(
                    {
                        "source": "Local",
                        "type": "hls" if ".m3u8" in rel.lower() else "mp4",
                        "url": base_url.rstrip("/") + rel if rel.startswith("/") else rel,
                    }
                )
            )
        yield ndjson.line(build_done_line(1 if rel else 0))

    return ndjson.response(_lines())
