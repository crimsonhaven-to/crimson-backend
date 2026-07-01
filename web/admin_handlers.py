"""Handlers the admin router pulls in via dependency injection.

These are NOT routes — they're the runtime/system snapshot, the source-health
probe sweep, and the forced metadata resync, wired into ``account_engine``'s admin
router by ``api.py`` (``set_system_handler`` / ``set_source_health_handler`` /
``set_resync_handler``). They live here rather than in ``admin_routes`` because
they read the VERSION, the scraper/resolver registries, the warm pool and the
scrape pipeline — and here rather than in ``api.py`` to keep the assembler thin.
Every DB-touching call hops the threadpool so we never block the event loop.
"""

import asyncio
import logging
import os
import platform
import time
from datetime import datetime, timezone
from typing import Dict, List

from starlette.concurrency import run_in_threadpool

from core.config import Config
from core.db_pool import pool_stats
from core.http_client import http_client
from core.version import PROCESS_STARTED_AT, VERSION
from core import source_health
from resolvers import ALL_RESOLVERS, _crimson_proxy
from resolvers.jellyfin import is_configured as jellyfin_is_configured
from scrapers import ALL_SCRAPERS
from local_engine.fs import is_configured as local_is_configured
from cache_engine.downloader import ffmpeg_available
from metadata_engine.anilist import fetch_anilist_metadata

from web.context import cache_store, db_engine, local_source_store
from web.pipeline import run_single_scraper

logger = logging.getLogger("crimson.admin")


# --- forced metadata resync -------------------------------------------------
# The admin "trigger metadata resync" endpoint runs the same forced Fribb rebuild
# as metadata_engine.resync, but in-process on the live db_engine (warm pool,
# MVCC-safe single transaction). Injected so admin_routes doesn't import the engine.
async def forced_resync() -> None:
    await db_engine.sync_database_async(force=True)


# --- runtime / system snapshot ----------------------------------------------
def _human_duration(seconds: float) -> str:
    """Compact uptime string, e.g. '3d 04h 12m'."""
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


async def admin_system_info() -> Dict:
    """A rich runtime snapshot for one replica: version + uptime, registry sizes,
    capability flags, DB-pool utilisation, and the server-side cache aggregate."""
    pool = await run_in_threadpool(pool_stats)
    cache_enabled = await run_in_threadpool(cache_store.get_enabled)
    cache_stats = await run_in_threadpool(cache_store.stats)
    cache_targets = await run_in_threadpool(cache_store.enabled_targets)
    local_sources = await run_in_threadpool(local_source_store.list_sources)
    local_enabled = sum(1 for s in local_sources if s.get("enabled"))
    # Live-ping the external CORS proxies (if any) so the dashboard shows which
    # are up. Cheap GET / health check per host; off entirely when unconfigured.
    # Uses refresh_health (not bare probe_bases) so opening the dashboard also
    # updates the routing health cache that drives automatic failover in proxy_url.
    proxy_hosts = await _crimson_proxy.refresh_health()

    now = time.time()
    return {
        "version": VERSION,
        "started_at": datetime.fromtimestamp(PROCESS_STARTED_AT, timezone.utc).isoformat(),
        "uptime_seconds": int(now - PROCESS_STARTED_AT),
        "uptime_human": _human_duration(now - PROCESS_STARTED_AT),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "registry": {
            "scrapers": len(ALL_SCRAPERS),
            "resolvers": len(ALL_RESOLVERS),
        },
        "flags": {
            "require_login": bool(getattr(Config, "REQUIRE_LOGIN", False)),
            "jellyfin_configured": jellyfin_is_configured(),
            "local_configured": local_is_configured(),
            "cache_enabled": bool(cache_enabled),
            "ffmpeg_available": ffmpeg_available(),
            "tmdb_key_set": bool(getattr(Config, "TMDB_API_KEY", None)),
            "rate_limit_storage": os.getenv("RATE_LIMIT_STORAGE_URI", "memory://"),
            "github_token_set": bool(os.getenv("GITHUB_TOKEN")),
            "crimson_proxy_enabled": _crimson_proxy.is_enabled(),
        },
        "proxies": {
            "enabled": _crimson_proxy.is_enabled(),
            "secret_set": bool(os.getenv("PROXY_SECRET")),
            "routed_sources": _crimson_proxy.ROUTED_SOURCES,
            "hosts": proxy_hosts,
        },
        "db_pool": pool,
        "cache": {
            "enabled": bool(cache_enabled),
            "targets_enabled": len(cache_targets),
            **cache_stats,
        },
        "local_sources": {"total": len(local_sources), "enabled": local_enabled},
    }


# --- source health ----------------------------------------------------------
# Probe every external scrape source against a known canary title (the real
# search→embeds pipeline, so green == would actually play), and report the
# operator-provided library sources' configuration. Results are cached for a few
# minutes so flipping to the dashboard tab doesn't re-hammer every upstream; the
# dashboard's "Re-probe" button passes force=True.
_SOURCE_HEALTH_TTL = float(os.getenv("SOURCE_HEALTH_TTL", "300"))
_source_health_cache: Dict[str, object] = {"at": 0.0, "data": None}
_source_health_lock = asyncio.Lock()


async def _probe_scrape_source(scraper_class, anilist_data: Dict) -> Dict:
    """End-to-end probe of one external source against the canary. Returns a row
    with status (ok/empty/error/disabled), latency, embed count and a human note."""
    name = scraper_class.__name__
    meta = source_health.meta_for(name)
    entry = {
        "id": name,
        "label": meta["label"],
        "category": "scrape",
        "note": meta.get("note"),
        "base_url": getattr(scraper_class, "BASE_URL", None),
        "supports_movies": bool(getattr(scraper_class, "SUPPORTS_MOVIES", False)),
        "latency_ms": None,
        "embeds": 0,
    }
    gate = meta.get("env_gate")
    if gate and not os.getenv(gate):
        entry.update(status="disabled", detail=f"{gate} not configured — source is dormant")
        return entry

    c = source_health.CANARY
    t0 = time.perf_counter()
    try:
        embeds = await run_single_scraper(
            scraper_class, c["tmdb_id"], c["season"], c["episode"], anilist_data,
            media_type="tv",
        )
        entry["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        n = len(embeds or [])
        entry["embeds"] = n
        if n > 0:
            entry.update(status="ok", detail=f"Resolved {n} embed(s) for the canary")
        else:
            entry.update(status="empty", detail="Reachable, but found no embeds for the canary title")
    except Exception as e:
        entry["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        entry.update(status="error", detail=(str(e)[:240] or e.__class__.__name__))
    return entry


async def _probe_library_sources() -> List[Dict]:
    """Config/occupancy status for the operator-provided sources (cache, local,
    Jellyfin). These only hold what the operator added, so they're reported by
    'is it set up and does it hold anything' rather than the canary probe."""
    out: List[Dict] = []

    cache_enabled = await run_in_threadpool(cache_store.get_enabled)
    cstats = await run_in_threadpool(cache_store.stats)
    targets = await run_in_threadpool(cache_store.enabled_targets)
    ready = cstats.get("ready") or 0
    if not cache_enabled:
        c_status, c_detail = "disabled", "Caching is switched off"
    elif ready > 0:
        c_status, c_detail = "active", f"{ready} episode(s) ready · {len(targets)} target(s)"
    else:
        c_status, c_detail = "idle", f"Enabled · {len(targets)} target(s), nothing cached yet"
    out.append({
        "id": "CacheScraper", "label": "Server Cache", "category": "library",
        "note": source_health.meta_for("CacheScraper").get("note"),
        "status": c_status, "detail": c_detail, "latency_ms": None,
        "metrics": {
            "ready": cstats.get("ready"), "pending": cstats.get("pending"),
            "downloading": cstats.get("downloading"), "failed": cstats.get("failed"),
            "targets": len(targets),
        },
    })

    local_sources = await run_in_threadpool(local_source_store.list_sources)
    enabled = [s for s in local_sources if s.get("enabled")]
    if not local_sources:
        l_status, l_detail = "disabled", "No local directories registered"
    elif enabled:
        l_status, l_detail = "active", f"{len(enabled)} of {len(local_sources)} directory(ies) enabled"
    else:
        l_status, l_detail = "idle", f"{len(local_sources)} directory(ies) registered, all disabled"
    out.append({
        "id": "LocalScraper", "label": "Local Media", "category": "library",
        "note": source_health.meta_for("LocalScraper").get("note"),
        "status": l_status, "detail": l_detail, "latency_ms": None,
        "metrics": {"total": len(local_sources), "enabled": len(enabled)},
    })

    jelly = jellyfin_is_configured()
    out.append({
        "id": "JellyfinScraper", "label": "Jellyfin", "category": "library",
        "note": source_health.meta_for("JellyfinScraper").get("note"),
        "status": "active" if jelly else "disabled",
        "detail": "Configured via JELLYFIN_* env" if jelly else "JELLYFIN_* env not set",
        "latency_ms": None, "metrics": {},
    })
    return out


async def _do_source_health() -> Dict:
    """Run the full probe sweep: shared canary metadata once, then every scrape
    source concurrently, plus the library sources. Assembles the summary tally."""
    anilist_data: Dict = {}
    try:
        async with http_client() as client:
            anilist_data = await fetch_anilist_metadata(client, source_health.CANARY["anilist_id"]) or {}
    except Exception as e:
        logger.warning(f"source-health canary metadata fetch failed: {e}")
    if not anilist_data.get("title"):
        anilist_data = {**anilist_data, "title": source_health.CANARY["title"]}

    scrape_classes = [
        c for c in ALL_SCRAPERS
        if source_health.meta_for(c.__name__)["category"] == "scrape"
    ]
    scrape_results = await asyncio.gather(
        *(_probe_scrape_source(c, anilist_data) for c in scrape_classes),
        return_exceptions=False,
    )
    library_results = await _probe_library_sources()
    sources = library_results + list(scrape_results)

    summary = {"total": len(sources)}
    for s in sources:
        summary[s["status"]] = summary.get(s["status"], 0) + 1
    # Latency stats over the scrape probes that actually ran.
    lats = [s["latency_ms"] for s in scrape_results if s.get("latency_ms") is not None]
    summary["avg_latency_ms"] = round(sum(lats) / len(lats)) if lats else None
    summary["slowest_ms"] = max(lats) if lats else None

    return {
        "canary": dict(source_health.CANARY),
        "sources": sources,
        "summary": summary,
    }


async def admin_source_health(force: bool = False) -> Dict:
    """Cached wrapper around the probe sweep (TTL ``SOURCE_HEALTH_TTL``). The lock
    collapses a thundering herd of dashboard loads into a single sweep."""
    now = time.monotonic()
    cached = _source_health_cache.get("data")
    if not force and cached and (now - float(_source_health_cache["at"]) < _SOURCE_HEALTH_TTL):
        return {**cached, "cached": True}

    async with _source_health_lock:
        now = time.monotonic()
        cached = _source_health_cache.get("data")
        if not force and cached and (now - float(_source_health_cache["at"]) < _SOURCE_HEALTH_TTL):
            return {**cached, "cached": True}
        data = await _do_source_health()
        data["probed_at"] = datetime.now(timezone.utc).isoformat()
        _source_health_cache["at"] = time.monotonic()
        _source_health_cache["data"] = data
        return {**data, "cached": False}
