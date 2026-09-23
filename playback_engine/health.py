"""The dashboard's Source Health view.

A ``scrape`` source is an external site, probed end to end against a canary
title through the real pipeline, so green means it would play right now. A
``library`` source holds only what the operator added, so a canary proves
nothing; it reports whether it is set up and non-empty instead. The base build
ships only library sources; an overlay scraper is probed as a scrape source.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Dict, List

from cache_engine.db import store as cache_store
from core.config import get_settings
from core.http_client import http_client
from local_engine.db import store as local_store
from metadata_engine.anilist import fetch_anilist_metadata
from resolvers.jellyfin import is_configured as jellyfin_is_configured
from scrapers import ALL_SCRAPERS

from .pipeline import run_single_scraper

logger = logging.getLogger("crimson.health")

LIBRARY_SOURCES = {
    "CacheScraper": {"label": "Server Cache", "note": "Remuxed episodes on your NAS"},
    "LocalScraper": {"label": "Local Media", "note": "Registered NAS / bind-mount dirs"},
    "JellyfinScraper": {"label": "Jellyfin", "note": "Your personal Jellyfin server"},
}

# Opening the tab must not re-hammer every upstream; "Re-probe" forces a sweep.
_TTL = 300.0
_cache: Dict[str, object] = {"at": 0.0, "data": None}
_lock = asyncio.Lock()


def canary() -> Dict:
    """The default carries both an AniList and a TMDB mapping, so every kind of
    scraper can attempt it."""
    s = get_settings()
    return {
        "title": s.health_canary_title,
        "tmdb_id": s.health_canary_tmdb,
        "season": s.health_canary_season,
        "episode": s.health_canary_episode,
        "anilist_id": s.health_canary_anilist,
    }


async def _probe_scrape_source(scraper_class, anilist_data: Dict, target: Dict) -> Dict:
    entry = {
        "id": scraper_class.__name__,
        "label": scraper_class.__name__,
        "category": "scrape",
        "note": None,
        "base_url": getattr(scraper_class, "BASE_URL", None),
        "supports_movies": bool(getattr(scraper_class, "SUPPORTS_MOVIES", False)),
        "latency_ms": None,
        "embeds": 0,
    }
    started = time.perf_counter()
    try:
        embeds = await run_single_scraper(
            scraper_class, target["tmdb_id"], target["season"], target["episode"], anilist_data, media_type="tv",
        )
        entry["embeds"] = len(embeds or [])
        if entry["embeds"]:
            entry.update(status="ok", detail=f"Resolved {entry['embeds']} embed(s) for the canary")
        else:
            entry.update(status="empty", detail="Reachable, but found no embeds for the canary title")
    except Exception as e:
        entry.update(status="error", detail=str(e)[:240] or e.__class__.__name__)
    entry["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return entry


def _library(source_id: str, status: str, detail: str, metrics: Dict) -> Dict:
    return {
        "id": source_id, "category": "library", **LIBRARY_SOURCES[source_id],
        "status": status, "detail": detail, "latency_ms": None, "metrics": metrics,
    }


def _probe_library_sources() -> List[Dict]:
    cache_on = cache_store.get_enabled()
    stats = cache_store.stats()
    targets = cache_store.enabled_targets()
    ready = stats.get("ready") or 0
    if not cache_on:
        cache = ("disabled", "Caching is switched off")
    elif ready:
        cache = ("active", f"{ready} episode(s) ready · {len(targets)} target(s)")
    else:
        cache = ("idle", f"Enabled · {len(targets)} target(s), nothing cached yet")

    sources = local_store.list_sources()
    enabled = [s for s in sources if s.get("enabled")]
    if not sources:
        local = ("disabled", "No local directories registered")
    elif enabled:
        local = ("active", f"{len(enabled)} of {len(sources)} directory(ies) enabled")
    else:
        local = ("idle", f"{len(sources)} directory(ies) registered, all disabled")

    jellyfin = jellyfin_is_configured()
    return [
        _library("CacheScraper", *cache, {
            "ready": stats.get("ready"), "pending": stats.get("pending"),
            "downloading": stats.get("downloading"), "failed": stats.get("failed"),
            "targets": len(targets),
        }),
        _library("LocalScraper", *local, {"total": len(sources), "enabled": len(enabled)}),
        _library(
            "JellyfinScraper",
            "active" if jellyfin else "disabled",
            "Configured via JELLYFIN_* env" if jellyfin else "JELLYFIN_* env not set",
            {},
        ),
    ]


async def _sweep() -> Dict:
    target = canary()
    anilist_data: Dict = {}
    try:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, target["anilist_id"]) or {}
    except Exception as e:
        logger.warning(f"source-health canary metadata fetch failed: {e}")
    anilist_data = {**anilist_data, "title": anilist_data.get("title") or target["title"]}

    scrape_results = await asyncio.gather(*(
        _probe_scrape_source(cls, anilist_data, target)
        for cls in ALL_SCRAPERS if cls.__name__ not in LIBRARY_SOURCES
    ))
    sources = await asyncio.to_thread(_probe_library_sources) + list(scrape_results)

    summary: Dict = {"total": len(sources)}
    for s in sources:
        summary[s["status"]] = summary.get(s["status"], 0) + 1
    latencies = [s["latency_ms"] for s in scrape_results]
    summary["avg_latency_ms"] = round(sum(latencies) / len(latencies)) if latencies else None
    summary["slowest_ms"] = max(latencies) if latencies else None
    return {"canary": target, "sources": sources, "summary": summary}


def _cached() -> Dict | None:
    if _cache["data"] and time.monotonic() - float(_cache["at"]) < _TTL:
        return {**_cache["data"], "cached": True}
    return None


async def source_health(force: bool = False) -> Dict:
    """Cached; the lock collapses a herd of dashboard loads into one sweep."""
    if not force and (hit := _cached()):
        return hit
    async with _lock:
        if not force and (hit := _cached()):
            return hit
        data = {**await _sweep(), "probed_at": datetime.now(timezone.utc).isoformat()}
        _cache.update(at=time.monotonic(), data=data)
        return {**data, "cached": False}
