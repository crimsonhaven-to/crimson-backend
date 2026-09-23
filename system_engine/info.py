"""One replica's runtime snapshot for the admin System tab."""

import asyncio
import platform
import time
from datetime import datetime, timezone
from typing import Dict

import resolvers
from cache_engine.db import store as cache_store
from cache_engine.downloader import ffmpeg_available
from core import migrations
from core.config import get_settings
from core.db_pool import pool_stats
from core.private_sources import discover_resolve_grants
from core.version import PROCESS_STARTED_AT, VERSION
from download_engine import aria2
from download_engine.db import store as download_store
from local_engine.db import store as local_store
from local_engine.fs import is_configured as local_is_configured
from resolvers import ALL_RESOLVERS, _crimson_proxy
from resolvers.jellyfin import is_configured as jellyfin_is_configured
from scrapers import ALL_SCRAPERS


def _human_duration(seconds: float) -> str:
    """e.g. '3d 04h 12m'."""
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m = s // 60
    if d:
        return f"{d}d {h:02d}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _database_state() -> Dict:
    sources = local_store.list_sources()
    return {
        "pool": pool_stats(),
        "cache_enabled": bool(cache_store.get_enabled()),
        "cache_stats": cache_store.stats(),
        "cache_targets": len(cache_store.enabled_targets()),
        "local_total": len(sources),
        "local_enabled": sum(1 for s in sources if s.get("enabled")),
        "download_sources": sum(1 for s in sources if s.get("download_enabled") and s.get("enabled")),
        "download_stats": download_store.stats(),
        # Live, unlike /health's boot snapshot: `pending` is a file in this image
        # not recorded here, `drift` an applied migration edited afterwards.
        "schema": migrations.status(),
    }


async def snapshot() -> Dict:
    db, aria2_ok, proxy_hosts = await asyncio.gather(
        asyncio.to_thread(_database_state),
        aria2.is_available(),
        # Refreshing rather than only probing also updates the failover cache
        # that proxy_url routes by.
        _crimson_proxy.refresh_health(),
    )
    settings = get_settings()
    flags = {
        "require_login": settings.require_login,
        "jellyfin_configured": jellyfin_is_configured(),
        "local_configured": local_is_configured(),
        "cache_enabled": db["cache_enabled"],
        "ffmpeg_available": ffmpeg_available(),
        "aria2_available": aria2_ok,
        "downloads_enabled_sources": db["download_sources"],
        "tmdb_key_set": bool(settings.tmdb_api_key),
        "rate_limit_storage": settings.rate_limit_storage_uri,
        "github_token_set": bool(settings.github_token),
        "crimson_proxy_enabled": _crimson_proxy.is_enabled(),
    }
    # Overlay sources name their own flags, so this module names none of them.
    for grant in discover_resolve_grants(resolvers):
        for flag, probe in (grant.get("admin_flags") or {}).items():
            try:
                flags[flag] = bool(probe())
            except Exception:
                flags[flag] = False

    uptime = time.time() - PROCESS_STARTED_AT
    return {
        "version": VERSION,
        "started_at": datetime.fromtimestamp(PROCESS_STARTED_AT, timezone.utc).isoformat(),
        "uptime_seconds": int(uptime),
        "uptime_human": _human_duration(uptime),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "registry": {"scrapers": len(ALL_SCRAPERS), "resolvers": len(ALL_RESOLVERS)},
        "flags": flags,
        "proxies": {
            "enabled": _crimson_proxy.is_enabled(),
            "secret_set": bool(settings.proxy_secret),
            "hosts": proxy_hosts,
        },
        "db_pool": db["pool"],
        "schema": db["schema"],
        "cache": {"enabled": db["cache_enabled"], "targets_enabled": db["cache_targets"], **db["cache_stats"]},
        "local_sources": {"total": db["local_total"], "enabled": db["local_enabled"]},
        "downloads": {"aria2_available": aria2_ok, "enabled_sources": db["download_sources"], **db["download_stats"]},
    }
