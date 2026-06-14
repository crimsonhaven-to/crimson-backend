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

from db_pool import get_connection
from rate_limit import limiter
from local_engine.db import LocalSourceStore
from local_engine.fs import inspect_path, discover_mountpoints
from .db import AccountStore
from .routes import require_user

router = APIRouter(prefix="/admin", tags=["admin"])
store = AccountStore()
local_store = LocalSourceStore()


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
