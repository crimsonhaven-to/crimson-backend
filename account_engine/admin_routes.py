"""
Admin API — a dashboard surface for accounts flagged ``is_admin``.

Everything here lives under ``/admin`` and is gated by ``require_admin`` (a valid
session whose account has the admin flag — see account_engine.db). The site-wide
login wall already blocks unauthenticated access; this adds the admin check on
top, so a normal signed-in user gets a 403, not a 401.

Capabilities (mirrors what the user asked for):
  * user management        — list / search, toggle admin & verified, revoke
                             sessions, delete accounts,
  * invite codes           — mint single-use invite tokens (same table the
                             Discord bot uses, see discord_bot/), list the
                             ledger, revoke unused ones,
  * metadata resync        — trigger a forced AniList<->TMDB Fribb resync in the
                             background (the same rebuild metadata_engine.resync
                             runs), with live status,
  * health / stats         — account-system + content (mapping) aggregates for a
                             dashboard.

The heavy mapping resync depends on the ``MappingDatabaseEngine`` that lives in
api.py, so rather than import it here (circular), api.py injects an async handler
via ``set_resync_handler`` at startup. Content/mapping stats are read straight
from the shared pool.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from core.config import Config
from core.db_pool import get_connection
from core.rate_limit import limiter
from metadata_engine import maintenance as metadata_maintenance
from local_engine.db import LocalSourceStore
from local_engine.fs import inspect_path, discover_mountpoints
from cache_engine.db import CacheStore
from cache_engine import fs as cache_fs
from cache_engine import downloader as cache_dl
from apikey_engine import store as apikey_store
from .db import AccountStore
from .routes import require_user

router = APIRouter(prefix="/admin", tags=["admin"])
store = AccountStore()
local_store = LocalSourceStore()
cache_store = CacheStore()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- admin gate ------------------------------------------------------------
def require_admin(user: dict = Depends(require_user)) -> dict:
    """Resolve the session (require_user) AND require the admin flag.

    ``require_user`` returns the full account row (``SELECT a.*``), which now
    carries ``is_admin``, so no extra query is needed."""
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def _public_user(row: Optional[dict]) -> Optional[dict]:
    """Strip secret/internal columns (password_hash, public_key) before returning
    an account row to the dashboard."""
    if not row:
        return None
    out = dict(row)
    out.pop("password_hash", None)
    pk = out.pop("public_key", None)
    out["has_mnemonic"] = pk is not None
    out.pop("session_expires_at", None)
    out["is_admin"] = bool(out.get("is_admin"))
    out["email_verified"] = bool(out.get("email_verified"))
    return out


# --- metadata resync (handler injected by api.py) --------------------------
_resync_lock = asyncio.Lock()
_resync_handler = None  # async callable () -> None, set by api.py
_resync_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "error": None,
    "triggered_by": None,
}


def set_resync_handler(handler) -> None:
    """Wire the forced-resync coroutine (api.py owns the MappingDatabaseEngine)."""
    global _resync_handler
    _resync_handler = handler


# --- injected handlers for the richer dashboard views ----------------------
# Both live in api.py (they need the scraper pipeline / runtime context), so they
# are injected here the same way the resync handler is — keeping admin_routes free
# of a circular import on api.py.
_system_handler = None        # async () -> dict   (runtime / pool / cache snapshot)
_source_health_handler = None  # async (force: bool) -> dict  (per-source probe)


def set_system_handler(handler) -> None:
    """Wire the runtime/system-info provider (api.py owns VERSION + the registries)."""
    global _system_handler
    _system_handler = handler


def set_source_health_handler(handler) -> None:
    """Wire the source-health prober (api.py owns the scraper/resolver pipeline)."""
    global _source_health_handler
    _source_health_handler = handler


async def _run_resync(triggered_by: str) -> None:
    # The lock makes a second trigger a no-op rebuild rather than two concurrent
    # Fribb downloads contending on the DB.
    async with _resync_lock:
        _resync_state.update(
            running=True, started_at=_now_iso(), finished_at=None,
            ok=None, error=None, triggered_by=triggered_by,
        )
        try:
            await _resync_handler()
            _resync_state["ok"] = True
        except Exception as e:  # surface the message to the dashboard
            _resync_state.update(ok=False, error=str(e))
        finally:
            _resync_state.update(running=False, finished_at=_now_iso())


# --- content (mapping) stats ----------------------------------------------
def _mapping_stats() -> dict:
    """Counts from the AniList<->TMDB mapping tables + last sync metadata. Each
    lookup is defensive so a missing table (fresh DB) yields null, not a 500."""
    out: dict = {}
    with get_connection() as conn:
        def count(table: str):
            try:
                return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            except Exception:
                return None

        out["anime_entries"] = count("anime_entries")
        out["tmdb_seasons"] = count("tmdb_seasons")
        out["tmdb_extras"] = count("tmdb_extras")
        out["tmdb_shows"] = count("tmdb_shows")
        out["tmdb_movies"] = count("tmdb_movies")
        out["api_cache"] = count("api_cache")
        try:
            row = conn.execute(
                "SELECT value FROM sync_meta WHERE key = 'etag'"
            ).fetchone()
            out["mapping_etag"] = row["value"] if row else None
        except Exception:
            out["mapping_etag"] = None
        try:
            row = conn.execute(
                "SELECT MAX(last_synced) AS m FROM anime_entries"
            ).fetchone()
            out["last_synced"] = row["m"] if row else None
        except Exception:
            out["last_synced"] = None
    return out


# --- stats / health --------------------------------------------------------
@router.get("/stats")
async def admin_stats(user: dict = Depends(require_admin)):
    """Account-system + content aggregates for the dashboard. (System info —
    scrapers/resolvers/jellyfin — is on the public /health endpoint the frontend
    also reads.)"""
    accounts = await run_in_threadpool(store.admin_overview)
    content = await run_in_threadpool(_mapping_stats)
    return {
        "success": True,
        "generated_at": _now_iso(),
        "accounts": accounts,
        "content": content,
        "resync": _resync_state,
    }


@router.get("/system")
async def admin_system(user: dict = Depends(require_admin)):
    """Rich runtime snapshot for the dashboard: version + uptime, the scraper/
    resolver registry sizes, capability flags, DB-pool utilisation and the
    server-side cache aggregate. Provided by api.py (it owns the registries)."""
    if _system_handler is None:
        raise HTTPException(status_code=503, detail="System info is not available on this node")
    info = await _system_handler()
    return {"success": True, "generated_at": _now_iso(), "system": info}


@router.get("/source-health")
async def admin_source_health(
    user: dict = Depends(require_admin),
    force: bool = Query(False, description="Bypass the short result cache and re-probe now"),
):
    """Per-source health: probe every external scrape source against a known canary
    title (green = embeds resolved, yellow = reachable but empty, red = error) and
    report the operator-provided library sources' configuration. Results are cached
    server-side for a few minutes; pass ``force=true`` to re-probe immediately.

    The probe runs the real search→embeds pipeline, so a green source is one that
    would actually play right now. Provided by api.py (it owns the pipeline)."""
    if _source_health_handler is None:
        raise HTTPException(status_code=503, detail="Source health is not available on this node")
    data = await _source_health_handler(force)
    return {"success": True, "generated_at": _now_iso(), **data}


# --- users -----------------------------------------------------------------
class UserUpdate(BaseModel):
    is_admin: Optional[bool] = None
    email_verified: Optional[bool] = None


@router.get("/users")
async def list_users(
    user: dict = Depends(require_admin),
    search: Optional[str] = Query(None, description="Match email / label / id"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    items = await run_in_threadpool(store.list_accounts, search, limit, offset)
    total = await run_in_threadpool(store.count_accounts, search)
    return {"success": True, "count": len(items), "total": total, "users": items}


@router.patch("/users/{user_id}")
async def update_user(user_id: int, body: UserUpdate, user: dict = Depends(require_admin)):
    """Toggle a user's admin / verified flags. You can't revoke your OWN admin
    flag (locking yourself out), nor demote the last remaining admin."""
    target = await run_in_threadpool(store.get_account, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if body.is_admin is not None and bool(target.get("is_admin")) != body.is_admin:
        if not body.is_admin:
            if user_id == user["user_id"]:
                raise HTTPException(status_code=400, detail="You cannot revoke your own admin access")
            if await run_in_threadpool(store.count_admins) <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last admin")
        await run_in_threadpool(store.set_admin, user_id, body.is_admin)

    if body.email_verified is not None:
        await run_in_threadpool(store.set_email_verified, user_id, body.email_verified)

    fresh = await run_in_threadpool(store.get_account, user_id)
    return {"success": True, "user": _public_user(fresh)}


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_user_sessions(user_id: int, user: dict = Depends(require_admin)):
    """Force-log-out a user by dropping all their active sessions."""
    target = await run_in_threadpool(store.get_account, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    await run_in_threadpool(store.revoke_user_sessions, user_id)
    return {"success": True, "user_id": user_id}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, user: dict = Depends(require_admin)):
    """Delete an account and (via ON DELETE CASCADE) its favorites / progress /
    sessions. You cannot delete your own account here."""
    if user_id == user["user_id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    removed = await run_in_threadpool(store.delete_account, user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="User not found")
    return {"success": True, "deleted": user_id}


# --- invite codes ----------------------------------------------------------
class InviteCreate(BaseModel):
    count: int = Field(1, ge=1, le=50)
    ttl_hours: Optional[int] = Field(None, ge=1, le=8760)  # max ~1 year


@router.get("/invites")
async def list_invites(
    user: dict = Depends(require_admin),
    include_used: bool = Query(True),
    limit: int = Query(100, ge=1, le=500),
):
    items = await run_in_threadpool(store.list_invite_tokens, include_used, limit)
    return {"success": True, "count": len(items), "invites": items}


@router.post("/invites")
@limiter.limit("30/minute")
async def create_invites(request: Request, body: InviteCreate, user: dict = Depends(require_admin)):
    """Mint ``count`` single-use invite codes (optionally expiring after
    ``ttl_hours``). Same table/contract the Discord bot uses, so the codes drop
    straight into the signup form's invite field."""
    ttl = timedelta(hours=body.ttl_hours) if body.ttl_hours else None
    created_by = f"admin:{user.get('email') or user['user_id']}"
    codes = [
        await run_in_threadpool(store.create_invite_token, created_by, ttl)
        for _ in range(body.count)
    ]
    return {"success": True, "count": len(codes), "codes": codes}


@router.delete("/invites/{code}")
async def revoke_invite(code: str, user: dict = Depends(require_admin)):
    ok = await run_in_threadpool(store.revoke_invite_token, code)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown or already-used invite code")
    return {"success": True, "revoked": code}


# --- movie-web bridge API keys ---------------------------------------------
# Admin-minted machine credentials handed to the modified movie-web fork. The
# fork's proxy injects the key server-side on calls to the /mw bridge endpoints
# (the key never reaches the browser); the login wall accepts it ONLY for /mw
# paths, so it can drive the bridge and nothing else. See apikey_engine/.
class ApiKeyCreate(BaseModel):
    label: Optional[str] = Field(None, max_length=100, description="A note to identify this key, e.g. 'movie-web prod'")


@router.get("/api-keys")
async def list_api_keys(
    user: dict = Depends(require_admin),
    include_revoked: bool = Query(True),
):
    """List minted bridge keys (never the raw secret — that's shown once, at
    creation). ``id`` is each key's handle for revocation."""
    items = await run_in_threadpool(apikey_store.list_keys, include_revoked)
    return {"success": True, "count": len(items), "keys": items}


@router.post("/api-keys")
@limiter.limit("30/minute")
async def create_api_key(request: Request, body: ApiKeyCreate, user: dict = Depends(require_admin)):
    """Mint a movie-web bridge key. The raw key is returned exactly ONCE in this
    response (only its hash is stored) — copy it into the fork's proxy secret now;
    it can't be retrieved later, only revoked + replaced."""
    created_by = f"admin:{user.get('email') or user['user_id']}"
    raw, info = await run_in_threadpool(apikey_store.create_key, (body.label or None), created_by)
    return {"success": True, "key": raw, "info": info}


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(key_id: str, user: dict = Depends(require_admin)):
    """Revoke a bridge key by its id. Takes effect within the login wall's
    validation-cache TTL (~60s)."""
    ok = await run_in_threadpool(apikey_store.revoke_key, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown or already-revoked API key")
    return {"success": True, "revoked": key_id}


# --- metadata resync -------------------------------------------------------
@router.get("/resync/status")
async def resync_status(user: dict = Depends(require_admin)):
    return {"success": True, "resync": _resync_state}


@router.post("/resync")
async def trigger_resync(user: dict = Depends(require_admin)):
    """Kick off a forced AniList<->TMDB mapping resync in the background (the same
    wholesale rebuild metadata_engine.resync runs). Returns immediately; poll
    /admin/resync/status for progress. A no-op if one is already running."""
    if _resync_handler is None:
        raise HTTPException(status_code=503, detail="Resync is not available on this node")
    if _resync_state["running"]:
        return {"success": False, "message": "A resync is already running", "resync": _resync_state}
    triggered_by = f"admin:{user.get('email') or user['user_id']}"
    asyncio.create_task(_run_resync(triggered_by))
    return {"success": True, "message": "Resync started", "resync": _resync_state}


# --- non-anime catalogue backfill (DB-queued, drained by api-sync) ----------
# Pages TMDB discover to pre-populate the tmdb_shows / tmdb_movies tables beyond
# what's been browsed (metadata_engine.maintenance.backfill_catalogue). This
# request usually lands on a serving replica, which can't reach the portless
# api-sync container that owns the heavy metadata work — so instead of running it
# here we ENQUEUE it (metadata_backfill_jobs) and let api-sync's drainer claim it.
# Status is read straight back from that row, so it's correct from any replica.
class BackfillTrigger(BaseModel):
    # Optional override; defaults to METADATA_BACKFILL_PAGES. TMDB discover caps at
    # page 500, and each page is ~20 rows, so this bounds how much gets seeded.
    pages: Optional[int] = Field(None, ge=1, le=500)


@router.get("/backfill/status")
async def backfill_status(user: dict = Depends(require_admin)):
    row = await run_in_threadpool(metadata_maintenance.latest_backfill_job)
    return {
        "success": True,
        "backfill": metadata_maintenance.job_status_payload(row),
        "default_pages": Config.METADATA_BACKFILL_PAGES,
    }


@router.post("/backfill")
async def trigger_backfill(body: Optional[BackfillTrigger] = None, user: dict = Depends(require_admin)):
    """Queue a non-anime catalogue backfill — page TMDB discover and lazily cache
    each (non-anime, postered) show/movie into tmdb_shows / tmdb_movies. The job is
    written to the DB and picked up within ~a minute by the api-sync container (so
    only that one container churns the metadata); poll /admin/backfill/status for
    progress. A no-op if one is already queued or running."""
    pages = body.pages if (body and body.pages) else Config.METADATA_BACKFILL_PAGES
    triggered_by = f"admin:{user.get('email') or user['user_id']}"
    row, created = await run_in_threadpool(metadata_maintenance.request_backfill, pages, triggered_by)
    payload = metadata_maintenance.job_status_payload(row)
    if not created:
        return {"success": False, "message": "A backfill is already queued or running", "backfill": payload}
    return {"success": True, "message": "Backfill queued — api-sync will start it shortly", "backfill": payload}


# --- local media sources (the "Local" direct-play source) ------------------
# CRUD for the directories the operator exposes to the haven (a NAS share or a
# Docker bind-mount, e.g. -v /movies:/crimson/movies1 -> register /crimson/movies1).
# The "Local" scraper streams browser-playable files straight off these roots.
class LocalSourceCreate(BaseModel):
    label: str = Field(..., min_length=1, max_length=100)
    path: str = Field(..., min_length=1, max_length=1000)


class LocalSourceUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None


def _local_with_status(row: dict) -> dict:
    """Merge a stored source row with a live filesystem probe for the dashboard."""
    out = dict(row)
    out["enabled"] = bool(out.get("enabled"))
    out["status"] = inspect_path(row["path"])
    return out


@router.get("/local-sources")
async def list_local_sources(user: dict = Depends(require_admin)):
    rows = await run_in_threadpool(local_store.list_sources)
    # inspect_path walks the tree (bounded) — do the whole list in one threadpool hop.
    items = await run_in_threadpool(lambda: [_local_with_status(r) for r in rows])
    return {"success": True, "count": len(items), "sources": items}


@router.get("/local-sources/discover")
async def discover_local_sources(user: dict = Depends(require_admin)):
    """Best-effort candidate directories (Docker bind-mounts / NAS mounts visible
    inside the container) the admin can add with one click. Advisory only."""
    mounts = await run_in_threadpool(discover_mountpoints)
    existing = await run_in_threadpool(local_store.list_sources)
    have = {os.path.normpath(r["path"]) for r in existing}
    for m in mounts:
        m["already_added"] = os.path.normpath(m["path"]) in have
    return {"success": True, "count": len(mounts), "mounts": mounts}


@router.post("/local-sources")
async def add_local_source(body: LocalSourceCreate, user: dict = Depends(require_admin)):
    """Register a directory. Validated up front (must be an absolute, existing,
    readable directory *inside the backend container*) so a wrong path / missing
    bind-mount fails loudly here instead of silently resolving nothing later."""
    path = os.path.normpath(body.path.strip())
    if not os.path.isabs(path):
        raise HTTPException(
            status_code=400,
            detail="Path must be absolute — the in-container path, e.g. /crimson/movies1",
        )
    info = await run_in_threadpool(inspect_path, path)
    if not info["exists"]:
        raise HTTPException(
            status_code=400,
            detail="Path does not exist inside the backend container. Bind-mount it in docker-compose first (e.g. - /movies:/crimson/movies1).",
        )
    if not info["is_dir"]:
        raise HTTPException(status_code=400, detail="Path is not a directory")
    if not info["readable"]:
        raise HTTPException(status_code=400, detail="Path is not readable by the backend")

    existing = await run_in_threadpool(local_store.list_sources)
    if any(os.path.normpath(r["path"]) == path for r in existing):
        raise HTTPException(status_code=409, detail="That path is already registered")

    row = await run_in_threadpool(local_store.add_source, body.label.strip(), path)
    return {"success": True, "source": await run_in_threadpool(_local_with_status, row)}


@router.patch("/local-sources/{source_id}")
async def update_local_source(source_id: int, body: LocalSourceUpdate, user: dict = Depends(require_admin)):
    """Toggle a source on/off or rename it (the path is immutable — delete + re-add)."""
    target = await run_in_threadpool(local_store.get_source, source_id)
    if not target:
        raise HTTPException(status_code=404, detail="Source not found")
    label = body.label.strip() if body.label is not None else None
    row = await run_in_threadpool(local_store.update_source, source_id, label, body.enabled)
    return {"success": True, "source": await run_in_threadpool(_local_with_status, row)}


@router.delete("/local-sources/{source_id}")
async def delete_local_source(source_id: int, user: dict = Depends(require_admin)):
    removed = await run_in_threadpool(local_store.delete_source, source_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Source not found")
    return {"success": True, "deleted": source_id}


# --- server-side video cache ------------------------------------------------
# A global on/off switch, the named NAS targets episodes are downloaded to, and a
# browsable ledger of what's been cached. When enabled, playing an episode kicks
# off a background full download (remuxed to mp4 with ffmpeg) to the first
# writable enabled target; on the next play the Cache source surfaces it, labelled
# with the target's name + the original language. See cache_engine/.
class CacheSettingsUpdate(BaseModel):
    enabled: bool


class CacheTargetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    path: str = Field(..., min_length=1, max_length=1000)


class CacheTargetUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None


def _cache_target_with_status(row: dict) -> dict:
    out = dict(row)
    out["enabled"] = bool(out.get("enabled"))
    out["status"] = cache_fs.inspect_target(row["path"])
    return out


@router.get("/cache")
async def cache_overview(user: dict = Depends(require_admin)):
    """Global cache status for the dashboard: master switch, ffmpeg availability,
    download config, and aggregate counts/bytes."""
    enabled = await run_in_threadpool(cache_store.get_enabled)
    stats = await run_in_threadpool(cache_store.stats)
    target_count = len(await run_in_threadpool(cache_store.enabled_targets))
    return {
        "success": True,
        "enabled": enabled,
        "ffmpeg_available": cache_dl.ffmpeg_available(),
        "enabled_targets": target_count,
        "stats": stats,
        "config": {
            "max_concurrent": cache_dl.MAX_CONCURRENT,
            "download_timeout": cache_dl.DOWNLOAD_TIMEOUT,
            "min_free_bytes": cache_dl.MIN_FREE_BYTES,
            "internal_base": cache_dl.INTERNAL_BASE,
        },
    }


@router.put("/cache/settings")
async def update_cache_settings(body: CacheSettingsUpdate, user: dict = Depends(require_admin)):
    """Flip the global cache master switch. With it off, no new downloads start;
    already-cached episodes keep playing as long as their target stays enabled."""
    enabled = await run_in_threadpool(cache_store.set_enabled, body.enabled)
    return {"success": True, "enabled": enabled}


@router.get("/cache-targets")
async def list_cache_targets(user: dict = Depends(require_admin)):
    rows = await run_in_threadpool(cache_store.list_targets)
    items = await run_in_threadpool(lambda: [_cache_target_with_status(r) for r in rows])
    return {"success": True, "count": len(items), "targets": items}


@router.get("/cache-targets/discover")
async def discover_cache_targets(user: dict = Depends(require_admin)):
    """Candidate NAS/bind-mount directories (probed for writability + free space)
    the admin can register with one click. Advisory only."""
    mounts = await run_in_threadpool(discover_mountpoints)
    existing = await run_in_threadpool(cache_store.list_targets)
    have = {os.path.normpath(r["path"]) for r in existing}

    def _enrich():
        out = []
        for m in mounts:
            entry = {"path": m["path"], "fstype": m.get("fstype")}
            entry.update(cache_fs.inspect_target(m["path"], count_cap=1))
            entry["already_added"] = os.path.normpath(m["path"]) in have
            out.append(entry)
        return out

    enriched = await run_in_threadpool(_enrich)
    return {"success": True, "count": len(enriched), "mounts": enriched}


@router.post("/cache-targets")
async def add_cache_target(body: CacheTargetCreate, user: dict = Depends(require_admin)):
    """Register a NAS directory as a cache target. Must be an absolute, existing,
    WRITABLE directory inside the backend container (bind-mount it first)."""
    path = os.path.normpath(body.path.strip())
    if not os.path.isabs(path):
        raise HTTPException(
            status_code=400,
            detail="Path must be absolute — the in-container path, e.g. /crimson/cache",
        )
    info = await run_in_threadpool(cache_fs.inspect_target, path, 1)
    if not info["exists"]:
        raise HTTPException(
            status_code=400,
            detail="Path does not exist inside the backend container. Bind-mount your NAS share first (e.g. - /nas/cache:/crimson/cache).",
        )
    if not info["is_dir"]:
        raise HTTPException(status_code=400, detail="Path is not a directory")
    if not info["writable"]:
        raise HTTPException(status_code=400, detail="Path is not writable by the backend")

    existing = await run_in_threadpool(cache_store.list_targets)
    if any(os.path.normpath(r["path"]) == path for r in existing):
        raise HTTPException(status_code=409, detail="That path is already registered")

    row = await run_in_threadpool(cache_store.add_target, body.name.strip(), path)
    return {"success": True, "target": await run_in_threadpool(_cache_target_with_status, row)}


@router.patch("/cache-targets/{target_id}")
async def update_cache_target(target_id: int, body: CacheTargetUpdate, user: dict = Depends(require_admin)):
    """Rename a target (its name is what viewers see as the source) or toggle it
    on/off. The path is immutable — delete + re-add to move it."""
    target = await run_in_threadpool(cache_store.get_target, target_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target not found")
    name = body.name.strip() if body.name is not None else None
    row = await run_in_threadpool(cache_store.update_target, target_id, name, body.enabled)
    return {"success": True, "target": await run_in_threadpool(_cache_target_with_status, row)}


@router.delete("/cache-targets/{target_id}")
async def delete_cache_target(target_id: int, user: dict = Depends(require_admin)):
    """Remove a target. Its cached_episodes rows cascade-delete; the files on the
    NAS are left in place (delete them on the share if you want the space back)."""
    removed = await run_in_threadpool(cache_store.delete_target, target_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Target not found")
    return {"success": True, "deleted": target_id}


@router.get("/cached-episodes")
async def list_cached_episodes(
    user: dict = Depends(require_admin),
    status: Optional[str] = Query(None, description="ready / pending / downloading / failed"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    items = await run_in_threadpool(cache_store.list_episodes, status, limit, offset)
    total = await run_in_threadpool(cache_store.count_episodes, status)
    return {"success": True, "count": len(items), "total": total, "episodes": items}


@router.delete("/cached-episodes/{entry_id}")
async def delete_cached_episode(entry_id: int, user: dict = Depends(require_admin)):
    """Drop a cache entry and delete its file from the NAS. Deleting a 'failed'
    entry also lets the episode be re-cached on its next play."""
    row = await run_in_threadpool(cache_store.delete_episode, entry_id)
    if not row:
        raise HTTPException(status_code=404, detail="Cache entry not found")

    def _unlink():
        target = cache_store.get_target(row["target_id"])
        if target:
            abs_path = os.path.join(target["path"], row["rel_path"])
            try:
                os.unlink(abs_path)
            except FileNotFoundError:
                pass
            except Exception:
                pass

    await run_in_threadpool(_unlink)
    return {"success": True, "deleted": entry_id}
