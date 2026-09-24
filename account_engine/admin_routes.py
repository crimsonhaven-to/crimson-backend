"""The admin dashboard's account side: overview stats, users, invites, the
broadcast email and the security ledger. The other engines mount their own
/admin routers behind the same ``require_admin``; the login wall already keeps
anonymous callers out, so a signed-in non-admin gets 403 here."""

import asyncio
from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from chat_engine.db import store as chat_store
from music_engine.db import store as music_store
from core.background import spawn
from core.clock import utc_now_iso
from core.rate_limit import limiter
from metadata_engine import forced_resync
from metadata_engine.catalogue import mapping_stats

from . import audit, mailer
from .audit import admin_identity, log_admin_action
from .db import store
from .deps import require_admin
from .schemas import BroadcastEmail, InviteCreate, UserUpdate

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


def _public_user(row: Optional[dict]) -> Optional[dict]:
    """A row without its secrets, as the dashboard shows it."""
    if not row:
        return None
    out = {
        k: v
        for k, v in row.items()
        if k not in ("password_hash", "public_key", "session_expires_at")
    }
    return {
        **out,
        "has_mnemonic": row.get("public_key") is not None,
        "is_admin": bool(row.get("is_admin")),
        "email_verified": bool(row.get("email_verified")),
        # Deny by default, so a row predating the column reads False, not None.
        "chat_enabled": bool(row.get("chat_enabled")),
        "music_enabled": bool(row.get("music_enabled")),
    }


async def _target_or_404(user_id: int) -> dict:
    target = await asyncio.to_thread(store.get_account, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    return target


# --- overview and the security ledger -------------------------------------------
@router.get("/stats")
async def admin_stats():
    accounts, content = await asyncio.gather(
        asyncio.to_thread(store.admin_overview), asyncio.to_thread(mapping_stats)
    )
    return {
        "success": True,
        "generated_at": utc_now_iso(),
        "accounts": accounts,
        "content": content,
        "resync": forced_resync.state,
    }


@router.get("/security/stats")
async def security_stats(
    days: int = Query(14, ge=1, le=90, description="Window for the chart/aggregates"),
):
    """24-hour tiles, a daily series, per-type totals, the top offending IPs and
    the most-targeted identities."""
    return {
        "success": True,
        "generated_at": utc_now_iso(),
        **await asyncio.to_thread(audit.stats, days),
    }


@router.get("/security/events")
async def security_events(
    event_type: Optional[str] = Query(None, description="Filter to one event type"),
    outcome: Optional[str] = Query(None, description="success / failure / info"),
    ip: Optional[str] = Query(None, description="Exact client IP"),
    search: Optional[str] = Query(None, description="Substring match on identity / IP"),
    hours: Optional[int] = Query(
        None, ge=1, le=2160, description="Only events from the last N hours"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """The raw ledger behind every number /admin/security/stats reports."""
    data = await asyncio.to_thread(
        audit.list_events, event_type, outcome, ip, search, hours, limit, offset
    )
    return {
        "success": True,
        "generated_at": utc_now_iso(),
        "count": len(data["events"]),
        "total": data["total"],
        "events": data["events"],
        "event_types": list(audit.EVENT_TYPES),
    }


# --- users -------------------------------------------------------------------------
@router.get("/users")
async def list_users(
    search: Optional[str] = Query(None, description="Match email / label / id"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    items, total = await asyncio.gather(
        asyncio.to_thread(store.list_accounts, search, limit, offset),
        asyncio.to_thread(store.count_accounts, search),
    )
    return {"success": True, "count": len(items), "total": total, "users": items}


def _apply_user_update(
    request: Request, admin: dict, user_id: int, target: dict, body: UserUpdate
) -> None:
    """Every change is audited. Nobody can revoke their own admin flag or demote
    the last admin. Chat and music access cost the operator money or disk, so
    both are audited like the admin flag rather than treated as preferences."""
    audit_target = {"target_user_id": user_id, "target": target.get("email")}
    if body.is_admin is not None and bool(target.get("is_admin")) != body.is_admin:
        if not body.is_admin:
            if user_id == admin["user_id"]:
                raise HTTPException(
                    status_code=400, detail="You cannot revoke your own admin access"
                )
            if store.count_admins() <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last admin")
        store.set_admin(user_id, body.is_admin)
        log_admin_action(
            request, admin, "admin_granted" if body.is_admin else "admin_revoked", **audit_target
        )

    if body.email_verified is not None:
        store.set_email_verified(user_id, body.email_verified)
        log_admin_action(
            request,
            admin,
            "verified_set" if body.email_verified else "verified_cleared",
            **audit_target,
        )

    if body.chat_enabled is not None and bool(target.get("chat_enabled")) != body.chat_enabled:
        chat_store.set_chat_access(user_id, body.chat_enabled)
        log_admin_action(
            request, admin, "chat_granted" if body.chat_enabled else "chat_revoked", **audit_target
        )

    if body.music_enabled is not None and bool(target.get("music_enabled")) != body.music_enabled:
        music_store.set_music_access(user_id, body.music_enabled)
        log_admin_action(
            request, admin, "music_granted" if body.music_enabled else "music_revoked",
            **audit_target,
        )

    if body.chat_budget_reset:
        chat_store.set_user_budget(user_id, None)
        log_admin_action(request, admin, "chat_budget_reset", **audit_target)
    elif body.chat_monthly_token_budget is not None:
        chat_store.set_user_budget(user_id, body.chat_monthly_token_budget)
        log_admin_action(
            request, admin, "chat_budget_set", budget=body.chat_monthly_token_budget, **audit_target
        )


@router.patch("/users/{user_id}")
async def update_user(
    request: Request, user_id: int, body: UserUpdate, user: dict = Depends(require_admin)
):
    target = await _target_or_404(user_id)
    await asyncio.to_thread(_apply_user_update, request, user, user_id, target, body)
    return {
        "success": True,
        "user": _public_user(await asyncio.to_thread(store.get_account, user_id)),
    }


@router.post("/users/{user_id}/revoke-sessions")
async def revoke_user_sessions(request: Request, user_id: int, user: dict = Depends(require_admin)):
    target = await _target_or_404(user_id)
    await asyncio.to_thread(store.revoke_user_sessions, user_id)
    log_admin_action(
        request, user, "sessions_revoked", target_user_id=user_id, target=target.get("email")
    )
    return {"success": True, "user_id": user_id}


@router.delete("/users/{user_id}")
async def delete_user(request: Request, user_id: int, user: dict = Depends(require_admin)):
    """Every row the account owns cascades with it."""
    if user_id == user["user_id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    target = await asyncio.to_thread(store.get_account, user_id)
    if not await asyncio.to_thread(store.delete_account, user_id):
        raise HTTPException(status_code=404, detail="User not found")
    log_admin_action(
        request, user, "user_deleted", target_user_id=user_id, target=(target or {}).get("email")
    )
    return {"success": True, "deleted": user_id}


# --- broadcast email -----------------------------------------------------------------
# One plaintext message to every account with an email, personalized with the
# display name, sent in the background over one SMTP connection. The dashboard
# polls the state.
_broadcast_lock = asyncio.Lock()
_broadcast_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "subject": None,
    "total": 0,
    "sent": 0,
    "failed": 0,
    "triggered_by": None,
}


async def _run_broadcast(recipients: list, subject: str, message: str) -> None:
    async with _broadcast_lock:

        def _progress(sent: int, failed: int) -> None:
            _broadcast_state.update(sent=sent, failed=failed)

        try:
            result = await asyncio.to_thread(
                mailer.send_broadcast, recipients, subject, message, _progress
            )
            _broadcast_state.update(sent=result["sent"], failed=result["failed"])
        except Exception:
            _broadcast_state["failed"] = len(recipients) - _broadcast_state["sent"]
        finally:
            _broadcast_state.update(running=False, finished_at=utc_now_iso())


@router.get("/broadcast-email")
async def broadcast_email_status():
    """Whether SMTP is configured (the form greys out when not), how many accounts
    a send reaches, and the current run."""
    counts = await asyncio.to_thread(store.email_recipient_counts)
    return {
        "success": True,
        "configured": mailer.is_configured(),
        "recipients": counts,
        "broadcast": _broadcast_state,
    }


@router.post("/broadcast-email")
@limiter.limit("5/minute")
async def send_broadcast_email(
    request: Request, body: BroadcastEmail, user: dict = Depends(require_admin)
):
    """Queued and answered at once; poll GET /admin/broadcast-email."""
    if not mailer.is_configured():
        raise HTTPException(
            status_code=503,
            detail="SMTP is not configured. Set SMTP_HOST and friends in the backend environment first.",
        )
    if _broadcast_state["running"]:
        return {
            "success": False,
            "message": "A broadcast is already being sent",
            "broadcast": _broadcast_state,
        }
    recipients = await asyncio.to_thread(store.email_recipients, body.verified_only)
    if not recipients:
        raise HTTPException(status_code=400, detail="No email accounts to send to")
    log_admin_action(
        request,
        user,
        "broadcast_email",
        subject=body.subject,
        recipients=len(recipients),
        verified_only=body.verified_only,
    )
    # Marked running here rather than in the task, so a double click cannot queue
    # a second send behind the lock and email everyone twice.
    subject = body.subject.strip()
    _broadcast_state.update(
        running=True,
        started_at=utc_now_iso(),
        finished_at=None,
        subject=subject,
        total=len(recipients),
        sent=0,
        failed=0,
        triggered_by=admin_identity(user),
    )
    spawn(_run_broadcast(recipients, subject, body.message))
    return {
        "success": True,
        "message": f"Sending to {len(recipients)} recipient{'s' if len(recipients) != 1 else ''}",
        "recipients": len(recipients),
        "broadcast": _broadcast_state,
    }


# --- invites -------------------------------------------------------------------------
@router.get("/invites")
async def list_invites(include_used: bool = Query(True), limit: int = Query(100, ge=1, le=500)):
    items = await asyncio.to_thread(store.list_invite_tokens, include_used, limit)
    return {"success": True, "count": len(items), "invites": items}


@router.post("/invites")
@limiter.limit("30/minute")
async def create_invites(request: Request, body: InviteCreate, user: dict = Depends(require_admin)):
    """Single-use codes, the same kind the Discord bot mints."""
    ttl = timedelta(hours=body.ttl_hours) if body.ttl_hours else None
    codes = await asyncio.to_thread(
        store.create_invite_tokens, body.count, admin_identity(user), ttl
    )
    log_admin_action(request, user, "invites_minted", count=len(codes), ttl_hours=body.ttl_hours)
    return {"success": True, "count": len(codes), "codes": codes}


@router.delete("/invites/{code}")
async def revoke_invite(request: Request, code: str, user: dict = Depends(require_admin)):
    if not await asyncio.to_thread(store.revoke_invite_token, code):
        raise HTTPException(status_code=404, detail="Unknown or already-used invite code")
    log_admin_action(request, user, "invite_revoked")
    return {"success": True, "revoked": code}
