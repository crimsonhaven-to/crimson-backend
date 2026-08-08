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
from core import prom_query
from core.rate_limit import limiter
from metadata_engine import maintenance as metadata_maintenance
from local_engine.db import LocalSourceStore
from local_engine.fs import inspect_path, discover_mountpoints
from local_engine.transcode import tools_available as encoding_tools_available
from cache_engine.db import CacheStore
from cache_engine import fs as cache_fs
from cache_engine import downloader as cache_dl
from download_engine.db import DownloadStore, KIND_HTTP, KIND_TORRENT
from download_engine import fs as download_fs
from download_engine import aria2 as download_aria2
from download_engine import manager as download_manager
from telemetry_engine import TelemetryStore
from apikey_engine import store as apikey_store
# The store, not the package: chat_engine/__init__ pulls in the chat routes,
# which import account_engine.routes, and importing the leaf directly keeps this
# module's import graph flat. chat_engine.db depends only on core.db_pool.
from chat_engine.db import ChatStore
from chat_engine.models import catalogue as chat_model_catalogue
from . import audit, mailer
from .db import AccountStore
from .routes import require_user

router = APIRouter(prefix="/admin", tags=["admin"])
store = AccountStore()
local_store = LocalSourceStore()
cache_store = CacheStore()
download_store = DownloadStore()
telemetry_store = TelemetryStore()
chat_store = ChatStore()


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


def _audit_admin(request: Optional[Request], user: dict, action: str, **detail) -> None:
    """Paper-trail a sensitive admin change as an ``admin_action`` security event
    (who did what to whom, from where). Fire-and-forget like all audit writes."""
    audit.log_event(
        "admin_action", outcome="success", request=request,
        user_id=user["user_id"],
        identity=f"admin:{user.get('email') or user['user_id']}",
        detail={"action": action, **{k: v for k, v in detail.items() if v is not None}},
    )


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
    # Lumi's chat grant. Deny-by-default, so a row predating the migration (or one
    # the column was never written for) reads as False rather than None.
    out["chat_enabled"] = bool(out.get("chat_enabled"))
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


@router.get("/source-stats")
async def admin_source_stats(
    user: dict = Depends(require_admin),
    days: int = Query(14, ge=1, le=365, description="Window to aggregate over"),
):
    """Real per-source resolve success rates from anonymous client beacons over the
    last ``days`` days. Complements /source-health (which probes from the backend):
    this reflects what actually resolved for viewers on the client+extension path —
    the visibility that was lost when resolving moved client-side."""
    rows = await run_in_threadpool(telemetry_store.top_stats, days)
    return {"success": True, "generated_at": _now_iso(), "days": days, "sources": rows}


# --- security event log ------------------------------------------------------
# The ledger the auth choke points, the 429 handler and the admin actions above
# write into (see account_engine.audit). Two reads: aggregate metrics for the
# dashboard's tiles/charts, and the filterable raw event table.
@router.get("/security/stats")
async def security_stats(
    user: dict = Depends(require_admin),
    days: int = Query(14, ge=1, le=90, description="Window for the chart/aggregates"),
):
    """Security metrics for the dashboard: 24h tiles (failed logins, invite
    rejections, rate-limit trips, distinct offending IPs), a zero-filled per-day
    event series, per-type totals, top offending IPs and the most-targeted
    identities over the last ``days`` days."""
    data = await run_in_threadpool(audit.stats, days)
    return {"success": True, "generated_at": _now_iso(), **data}


@router.get("/security/events")
async def security_events(
    user: dict = Depends(require_admin),
    event_type: Optional[str] = Query(None, description="Filter to one event type"),
    outcome: Optional[str] = Query(None, description="success / failure / info"),
    ip: Optional[str] = Query(None, description="Exact client IP"),
    search: Optional[str] = Query(None, description="Substring match on identity / IP"),
    hours: Optional[int] = Query(None, ge=1, le=2160, description="Only events from the last N hours"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """The raw ledger, newest first, filterable — the drill-down behind every
    number /admin/security/stats shows."""
    data = await run_in_threadpool(
        lambda: audit.list_events(event_type, outcome, ip, search, hours, limit, offset)
    )
    return {
        "success": True,
        "generated_at": _now_iso(),
        "count": len(data["events"]),
        "total": data["total"],
        "events": data["events"],
        "event_types": list(audit.EVENT_TYPES),
    }


# --- users -----------------------------------------------------------------
class UserUpdate(BaseModel):
    is_admin: Optional[bool] = None
    email_verified: Optional[bool] = None
    # Lumi's chat grant, and an optional per-account monthly token ceiling. The
    # budget is tri-state on purpose: absent leaves it alone, a number sets it,
    # and an explicit 0 freezes this user without revoking the grant. Clearing it
    # back to the global default is a separate flag rather than null, because a
    # JSON null is indistinguishable from an omitted field here.
    chat_enabled: Optional[bool] = None
    chat_monthly_token_budget: Optional[int] = Field(None, ge=0)
    chat_budget_reset: Optional[bool] = None


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
async def update_user(request: Request, user_id: int, body: UserUpdate, user: dict = Depends(require_admin)):
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
        _audit_admin(request, user, "admin_granted" if body.is_admin else "admin_revoked",
                     target_user_id=user_id, target=target.get("email"))

    if body.email_verified is not None:
        await run_in_threadpool(store.set_email_verified, user_id, body.email_verified)
        _audit_admin(request, user, "verified_set" if body.email_verified else "verified_cleared",
                     target_user_id=user_id, target=target.get("email"))

    # Granting chat access is a spending decision, so it is audited exactly like
    # the admin flag above rather than treated as a cosmetic preference.
    if body.chat_enabled is not None and bool(target.get("chat_enabled")) != body.chat_enabled:
        await run_in_threadpool(chat_store.set_chat_access, user_id, body.chat_enabled)
        _audit_admin(request, user,
                     "chat_granted" if body.chat_enabled else "chat_revoked",
                     target_user_id=user_id, target=target.get("email"))

    if body.chat_budget_reset:
        await run_in_threadpool(chat_store.set_user_budget, user_id, None)
        _audit_admin(request, user, "chat_budget_reset",
                     target_user_id=user_id, target=target.get("email"))
    elif body.chat_monthly_token_budget is not None:
        await run_in_threadpool(
            chat_store.set_user_budget, user_id, body.chat_monthly_token_budget
        )
        _audit_admin(request, user, "chat_budget_set",
                     target_user_id=user_id, target=target.get("email"),
                     budget=body.chat_monthly_token_budget)

    fresh = await run_in_threadpool(store.get_account, user_id)
    return {"success": True, "user": _public_user(fresh)}


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_user_sessions(request: Request, user_id: int, user: dict = Depends(require_admin)):
    """Force-log-out a user by dropping all their active sessions."""
    target = await run_in_threadpool(store.get_account, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    await run_in_threadpool(store.revoke_user_sessions, user_id)
    _audit_admin(request, user, "sessions_revoked", target_user_id=user_id, target=target.get("email"))
    return {"success": True, "user_id": user_id}


@router.delete("/users/{user_id}")
async def delete_user(request: Request, user_id: int, user: dict = Depends(require_admin)):
    """Delete an account and (via ON DELETE CASCADE) its favorites / progress /
    sessions. You cannot delete your own account here."""
    if user_id == user["user_id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    target = await run_in_threadpool(store.get_account, user_id)
    removed = await run_in_threadpool(store.delete_account, user_id)
    if not removed:
        raise HTTPException(status_code=404, detail="User not found")
    _audit_admin(request, user, "user_deleted", target_user_id=user_id,
                 target=(target or {}).get("email"))
    return {"success": True, "deleted": user_id}


# --- broadcast email ---------------------------------------------------------
# The Users tab's "E-Mail sender": one plaintext message, fanned out to every
# account that signed up with an email address (mnemonic-only accounts have no
# address and are skipped), personalised with the account's display name. Sending
# happens in the background over one SMTP connection (mailer.send_broadcast);
# this state dict mirrors _resync_state so the dashboard can poll live progress.
_broadcast_lock = asyncio.Lock()
_broadcast_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "subject": None,
    "total": 0,
    "sent": 0,
    "failed": 0,
    "triggered_by": None,
}


class BroadcastEmail(BaseModel):
    subject: str = Field(..., min_length=1, max_length=200)
    message: str = Field(..., min_length=1, max_length=20000)
    # Skip addresses that never clicked their verification link (they may not
    # even belong to the account holder). The dashboard surfaces the toggle.
    verified_only: bool = True


async def _run_broadcast(recipients: list, subject: str, message: str) -> None:
    async with _broadcast_lock:
        def _progress(sent: int, failed: int) -> None:
            _broadcast_state.update(sent=sent, failed=failed)

        try:
            result = await run_in_threadpool(
                mailer.send_broadcast, recipients, subject, message, _progress
            )
            _broadcast_state.update(sent=result["sent"], failed=result["failed"])
        except Exception:  # send_broadcast fails soft; this is a belt-and-braces net
            _broadcast_state["failed"] = len(recipients) - _broadcast_state["sent"]
        finally:
            _broadcast_state.update(running=False, finished_at=_now_iso())


@router.get("/broadcast-email")
async def broadcast_email_status(user: dict = Depends(require_admin)):
    """Everything the E-Mail sender panel needs up front: whether SMTP is
    configured at all (the form greys out when it isn't), how many accounts a
    send would reach, and the live/last run's progress."""
    counts = {
        "verified": len(await run_in_threadpool(store.email_recipients, True)),
        "all": len(await run_in_threadpool(store.email_recipients, False)),
    }
    return {
        "success": True,
        "configured": mailer.is_configured(),
        "recipients": counts,
        "broadcast": _broadcast_state,
    }


@router.post("/broadcast-email")
@limiter.limit("5/minute")
async def send_broadcast_email(request: Request, body: BroadcastEmail, user: dict = Depends(require_admin)):
    """Queue the broadcast and return immediately; poll GET /admin/broadcast-email
    for progress. Refused cleanly (not a 500) when SMTP isn't configured, when a
    send is already running, or when there's nobody to email."""
    if not mailer.is_configured():
        raise HTTPException(
            status_code=503,
            detail="SMTP is not configured — set SMTP_HOST (and friends) in the backend environment first.",
        )
    if _broadcast_state["running"]:
        return {"success": False, "message": "A broadcast is already being sent", "broadcast": _broadcast_state}
    recipients = await run_in_threadpool(store.email_recipients, body.verified_only)
    if not recipients:
        raise HTTPException(status_code=400, detail="No email accounts to send to")
    triggered_by = f"admin:{user.get('email') or user['user_id']}"
    _audit_admin(request, user, "broadcast_email", subject=body.subject,
                 recipients=len(recipients), verified_only=body.verified_only)
    # Flip the state HERE (not in the task) so a double-click can't queue a second
    # send behind the lock and email everyone twice.
    _broadcast_state.update(
        running=True, started_at=_now_iso(), finished_at=None,
        subject=body.subject.strip(), total=len(recipients), sent=0, failed=0,
        triggered_by=triggered_by,
    )
    asyncio.create_task(_run_broadcast(recipients, body.subject.strip(), body.message))
    return {
        "success": True,
        "message": f"Sending to {len(recipients)} recipient{'s' if len(recipients) != 1 else ''}",
        "recipients": len(recipients),
        "broadcast": _broadcast_state,
    }


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
    _audit_admin(request, user, "invites_minted", count=len(codes), ttl_hours=body.ttl_hours)
    return {"success": True, "count": len(codes), "codes": codes}


@router.delete("/invites/{code}")
async def revoke_invite(request: Request, code: str, user: dict = Depends(require_admin)):
    ok = await run_in_threadpool(store.revoke_invite_token, code)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown or already-used invite code")
    _audit_admin(request, user, "invite_revoked")
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
    _audit_admin(request, user, "api_key_created", label=body.label)
    return {"success": True, "key": raw, "info": info}


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(request: Request, key_id: str, user: dict = Depends(require_admin)):
    """Revoke a bridge key by its id. Takes effect within the login wall's
    validation-cache TTL (~60s)."""
    ok = await run_in_threadpool(apikey_store.revoke_key, key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Unknown or already-revoked API key")
    _audit_admin(request, user, "api_key_revoked", key_id=key_id)
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
    # On-the-fly HLS transcoding for non-web containers (mkv/avi/…). Off by default
    # so a new source is direct-play-only until the operator opts in.
    encoding: bool = False
    # Whether the background downloader may write into this root (under
    # crimson-downloads/). Off by default — the operator opts a source in.
    download_enabled: bool = False


class LocalSourceUpdate(BaseModel):
    label: Optional[str] = Field(None, min_length=1, max_length=100)
    enabled: Optional[bool] = None
    encoding: Optional[bool] = None
    download_enabled: Optional[bool] = None


def _local_with_status(row: dict) -> dict:
    """Merge a stored source row with a live filesystem probe for the dashboard.
    Download-enabled roots additionally carry a free-space/occupancy probe of their
    crimson-downloads dir so the dashboard can show which one the downloader will pick."""
    out = dict(row)
    out["enabled"] = bool(out.get("enabled"))
    out["encoding"] = bool(out.get("encoding"))
    out["download_enabled"] = bool(out.get("download_enabled"))
    out["status"] = inspect_path(row["path"])
    if out["download_enabled"]:
        out["downloads"] = download_fs.inspect_downloads(row["path"])
    return out


@router.get("/local-sources")
async def list_local_sources(user: dict = Depends(require_admin)):
    rows = await run_in_threadpool(local_store.list_sources)
    # inspect_path walks the tree (bounded) — do the whole list in one threadpool hop.
    items = await run_in_threadpool(lambda: [_local_with_status(r) for r in rows])
    # encoding_supported tells the dashboard whether to offer the per-source encoding
    # toggle at all: the on-the-fly HLS transcode needs ffmpeg AND ffprobe in the
    # image, so grey the switch out when they're missing rather than failing playback.
    return {
        "success": True,
        "count": len(items),
        "sources": items,
        "encoding_supported": encoding_tools_available(),
    }


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

    row = await run_in_threadpool(
        local_store.add_source, body.label.strip(), path, body.encoding, body.download_enabled
    )
    return {"success": True, "source": await run_in_threadpool(_local_with_status, row)}


@router.patch("/local-sources/{source_id}")
async def update_local_source(source_id: int, body: LocalSourceUpdate, user: dict = Depends(require_admin)):
    """Toggle a source on/off, flip its encoding (transcoding) switch, or rename it
    (the path is immutable — delete + re-add)."""
    target = await run_in_threadpool(local_store.get_source, source_id)
    if not target:
        raise HTTPException(status_code=404, detail="Source not found")
    label = body.label.strip() if body.label is not None else None
    row = await run_in_threadpool(
        local_store.update_source, source_id, label, body.enabled, body.encoding,
        body.download_enabled,
    )
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


# --- background downloader (aria2) ------------------------------------------
# Admin-submitted downloads: a plain http/https URL or a magnet link, fetched in
# the background by the aria2 sidecar and landed under <root>/crimson-downloads/ on
# the first *download-enabled* local source with free space. Once on disk, the
# local library scanner surfaces it like any other on-disk title. See
# download_engine/.
class DownloadCreate(BaseModel):
    url: str = Field(..., min_length=1, max_length=8000, description="An http(s) URL or a magnet: link")
    # Optional title — becomes the crimson-downloads/<name>/ folder, which greatly
    # helps the library scanner identify the download. Omit to keep the source's own
    # file/release name.
    name: Optional[str] = Field(None, max_length=180)


def _classify_download_url(url: str) -> str:
    """Map a submitted URL to a download kind, or raise a 400 with guidance."""
    u = url.strip()
    low = u.lower()
    if low.startswith("magnet:"):
        return KIND_TORRENT
    if low.startswith(("http://", "https://")):
        if low.split("?", 1)[0].endswith(".torrent"):
            raise HTTPException(
                status_code=400,
                detail="Paste the magnet link instead of a .torrent file URL.",
            )
        return KIND_HTTP
    raise HTTPException(
        status_code=400,
        detail="URL must be an http(s):// link or a magnet: link.",
    )


def _job_public(row: dict) -> dict:
    """Shape a download_jobs row for the dashboard (drops nothing sensitive — it's
    admin-only — but normalises the numeric/progress fields)."""
    out = dict(row)
    total = out.get("bytes_total")
    done = out.get("bytes_done") or 0
    out["bytes_done"] = int(done)
    out["bytes_total"] = int(total) if total else None
    out["download_speed"] = int(out.get("download_speed") or 0)
    out["progress"] = (done / total) if (total and total > 0) else None
    return out


@router.get("/downloads")
async def downloads_overview(user: dict = Depends(require_admin)):
    """Downloader status for the dashboard: aria2 availability, config, aggregate
    job counts, and the download-enabled roots with their free space (the order the
    downloader tries them)."""
    aria2_ok = await download_aria2.is_available()
    stats = await run_in_threadpool(download_store.stats)
    roots = await run_in_threadpool(local_store.download_roots_config)

    def _root_view():
        out = []
        for r in roots:
            out.append({
                "id": r["id"],
                "label": r.get("label"),
                "path": r["path"],
                **download_fs.inspect_downloads(r["path"]),
            })
        return out

    targets = await run_in_threadpool(_root_view)
    return {
        "success": True,
        "aria2_available": aria2_ok,
        "aria2_rpc_url": download_aria2.RPC_URL,
        "download_targets": targets,
        "stats": stats,
        "config": {
            "max_active": download_manager.MAX_ACTIVE,
            "min_free_bytes": download_manager.MIN_FREE_BYTES,
            "poll_interval": download_manager.POLL_INTERVAL,
        },
    }


@router.get("/download-jobs")
async def list_download_jobs(
    user: dict = Depends(require_admin),
    status: Optional[str] = Query(None, description="pending / active / paused / complete / failed"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    rows = await run_in_threadpool(download_store.list_jobs, status, limit, offset)
    total = await run_in_threadpool(download_store.count_jobs, status)
    return {
        "success": True,
        "count": len(rows),
        "total": total,
        "jobs": [_job_public(r) for r in rows],
    }


@router.post("/downloads")
@limiter.limit("60/minute")
async def create_download(request: Request, body: DownloadCreate, user: dict = Depends(require_admin)):
    """Queue a download. Rejected up front when no local source is download-enabled
    (turn one on under Local Sources first) so the operator gets a clear error instead
    of a job that silently never lands anywhere."""
    kind = _classify_download_url(body.url)
    roots = await run_in_threadpool(local_store.download_roots_config)
    if not roots:
        raise HTTPException(
            status_code=400,
            detail="No local source is download-enabled. Enable one under Local Sources first.",
        )
    created_by = f"admin:{user.get('email') or user['user_id']}"
    name = (body.name or "").strip() or None
    row = await run_in_threadpool(
        download_store.create_job, kind, body.url.strip(), name, created_by
    )
    return {"success": True, "job": _job_public(row)}


@router.post("/download-jobs/{job_id}/pause")
async def pause_download(job_id: int, user: dict = Depends(require_admin)):
    job = await run_in_threadpool(download_store.get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download not found")
    row = await download_manager.pause_job(download_store, job)
    return {"success": True, "job": _job_public(row) if row else None}


@router.post("/download-jobs/{job_id}/resume")
async def resume_download(job_id: int, user: dict = Depends(require_admin)):
    job = await run_in_threadpool(download_store.get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download not found")
    row = await download_manager.resume_job(download_store, job)
    return {"success": True, "job": _job_public(row) if row else None}


@router.post("/download-jobs/{job_id}/retry")
async def retry_download(job_id: int, user: dict = Depends(require_admin)):
    """Re-queue a failed (or stuck) download. Its staging dir is left in place so
    aria2 resumes from the partial rather than starting over."""
    job = await run_in_threadpool(download_store.get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Download not found")
    row = await run_in_threadpool(download_store.requeue, job_id)
    return {"success": True, "job": _job_public(row) if row else None}


@router.delete("/download-jobs/{job_id}")
async def delete_download(job_id: int, user: dict = Depends(require_admin)):
    """Cancel + remove a download. Stops it in aria2 and deletes any in-progress
    staging files; a *completed* download's published file under crimson-downloads is
    left in place (delete it from the Local library if you want the space back)."""
    row = await run_in_threadpool(download_store.delete_job, job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")
    await download_manager.cancel_and_delete_job(download_store, row)
    return {"success": True, "deleted": job_id}


# --- metrics history (Prometheus) -------------------------------------------
# The time axis behind the Metrics tab. /metrics itself is a live snapshot of one
# replica; these three read a private Prometheus that scrapes every replica, so
# they can answer "what did the fleet do over the last week" instead.
#
# The catalogue of panels and the PromQL behind each one live in core/prom_query.py
# and are NOT client-supplied: the browser sends a panel id, which is a dictionary
# key. See the module docstring there for why.


@router.get("/metrics/panels")
async def metrics_panels(user: dict = Depends(require_admin)):
    """What the history section can draw, and whether it can draw anything at all.

    ``available: false`` is the normal answer for a deploy without Prometheus, and
    the dashboard degrades to the live snapshot rather than showing errors, so this
    is a plain fact about the environment and not a failure."""
    if not prom_query.available():
        return {
            "success": True,
            "available": False,
            "reason": "PROMETHEUS_URL is not set, so no history is being collected",
            "panels": [],
            "ranges": [],
        }
    return {
        "success": True,
        "available": True,
        "job": prom_query.job_label(),
        "retention": await prom_query.retention_hint(),
        "panels": prom_query.panel_catalogue(),
        "ranges": prom_query.range_catalogue(),
        "default_range": prom_query.DEFAULT_RANGE,
    }


@router.get("/metrics/series")
@limiter.limit("240/minute")
async def metrics_series(
    request: Request,
    panel: str = Query(..., description="Panel id from /admin/metrics/panels"),
    # Aliased rather than named `range` so the handler does not shadow the builtin
    # while the query string still reads the way the client writes it.
    range_id: str = Query(prom_query.DEFAULT_RANGE, alias="range", description="Range id from /admin/metrics/panels"),
    user: dict = Depends(require_admin),
):
    """One panel's timeseries.

    The rate limit is generous because opening the tab fires one of these per
    panel, and changing the range refires all of them; it is here to stop a stuck
    client looping on Prometheus, not to pace normal use."""
    if not prom_query.available():
        raise HTTPException(status_code=503, detail="No Prometheus is configured (set PROMETHEUS_URL)")
    if panel not in prom_query.PANELS:
        raise HTTPException(status_code=404, detail=f"Unknown panel '{panel[:40]}'")
    if range_id not in prom_query.RANGES:
        raise HTTPException(status_code=404, detail=f"Unknown range '{range_id[:40]}'")
    return await prom_query.query_panel(panel, range_id)


@router.get("/metrics/targets")
async def metrics_targets(user: dict = Depends(require_admin)):
    """The replicas Prometheus is scraping, so an empty chart can be told apart
    from a scraper that has lost the fleet."""
    if not prom_query.available():
        raise HTTPException(status_code=503, detail="No Prometheus is configured (set PROMETHEUS_URL)")
    return await prom_query.scrape_targets()


# --- Lumi's chatbot --------------------------------------------------------
# Operator control for chat_engine: whether the feature is on, which provider and
# model answers, and what it has cost. Per-account grants are NOT here; they ride
# on PATCH /users/{id} alongside the admin flag, because granting a person access
# is a user-management action.
#
# API keys are deliberately absent from both the read and the write path. They
# live in the environment (ANTHROPIC_API_KEY / GEMINI_API_KEY) so a database dump
# never carries billable credentials; the dashboard is told only whether each one
# is present.

class ChatSettingsUpdate(BaseModel):
    enabled: Optional[bool] = None
    provider: Optional[str] = Field(None, pattern="^(anthropic|gemini)$")
    model: Optional[str] = None
    monthly_token_budget: Optional[int] = Field(None, ge=0)
    history_turns: Optional[int] = Field(None, ge=1, le=50)
    max_tool_iterations: Optional[int] = Field(None, ge=1, le=10)


def _chat_settings_payload() -> dict:
    """Settings plus the environment facts the dashboard needs to render them."""
    from chat_engine.routes import provider_key
    from chat_engine.providers import ANTHROPIC_SDK_AVAILABLE

    settings = chat_store.get_settings()
    return {
        "settings": settings,
        "models": chat_model_catalogue(),
        "keys": {
            # Presence only. The values never leave the process.
            "anthropic": bool(provider_key("anthropic")),
            "gemini": bool(provider_key("gemini")),
        },
        "sdk": {"anthropic": ANTHROPIC_SDK_AVAILABLE},
    }


@router.get("/chat/settings")
async def chat_settings(user: dict = Depends(require_admin)):
    return await run_in_threadpool(_chat_settings_payload)


@router.patch("/chat/settings")
async def update_chat_settings(
    request: Request, body: ChatSettingsUpdate, user: dict = Depends(require_admin)
):
    """Change provider, model, budgets or the master switch.

    Switching the feature on without a key for the selected provider is rejected
    rather than accepted-and-broken: the failure would otherwise only surface as
    a 503 to whichever viewer opened the drawer first.
    """
    from chat_engine.routes import provider_key

    patch = body.model_dump(exclude_none=True)
    current = await run_in_threadpool(chat_store.get_settings)
    target_provider = patch.get("provider", current["provider"])

    if patch.get("enabled") and not provider_key(target_provider):
        raise HTTPException(
            status_code=400,
            detail=f"No API key configured for {target_provider}. Set it in the environment first.",
        )

    # A model id belonging to the other vendor is a mistake worth naming, rather
    # than silently falling back to that provider's default at request time.
    if "model" in patch:
        from chat_engine.models import get_model

        model = get_model(patch["model"])
        if model is None:
            raise HTTPException(status_code=400, detail=f"Unknown model '{patch['model'][:60]}'")
        if model.provider != target_provider:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{model.model_id}' belongs to {model.provider}, not {target_provider}",
            )

    await run_in_threadpool(chat_store.update_settings, patch, updated_by=user["user_id"])
    _audit_admin(request, user, "chat_settings_updated", **patch)
    return await run_in_threadpool(_chat_settings_payload)


@router.get("/chat/usage")
async def chat_usage(
    user: dict = Depends(require_admin),
    days: int = Query(30, ge=1, le=365, description="Aggregation window"),
):
    """Token and estimated-cost totals, plus the biggest spenders.

    Cost is an estimate computed from published per-million rates at call time
    (see chat_engine.models), not a figure from the provider's billing API, so it
    tracks real spend closely but will not match a vendor invoice to the cent.
    """
    return await run_in_threadpool(chat_store.usage_overview, days)
