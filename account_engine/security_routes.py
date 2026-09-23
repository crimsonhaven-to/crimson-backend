"""The caller's own sessions, security events, data export and deletion.

A session is addressed by a one-way derived id, never by ``token_hash``
(account_engine.db explains why). Events are filtered on ``user_id`` alone and
carry neither ``detail`` nor ``identity``.
"""

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from core.clock import utc_now, utc_now_iso
from core.config import get_settings
from core.rate_limit import limiter
from notify_engine.db import store as airing_store

from . import audit, auth
from .db import store
from .deps import bearer_token, require_user
from .schemas import DeleteAccountRequest

router = APIRouter(tags=["account-security"])

# Enough to cover "what happened while I was away" without paging a table that
# has no per-user cursor.
_EVENT_LIMIT = 100


@router.get("/account/sessions")
async def list_sessions(
    user: dict = Depends(require_user),
    token: Optional[str] = Depends(bearer_token),
):
    """``user_agent`` and ``ip`` are null for sessions older than migration 005.
    They are listed anyway: a session you cannot place is the one most worth
    seeing."""
    sessions = await run_in_threadpool(store.list_sessions, user["user_id"], token)
    return {"success": True, "count": len(sessions), "sessions": sessions}


@router.delete("/account/sessions/{session_id}")
async def revoke_session(
    request: Request,
    session_id: str,
    user: dict = Depends(require_user),
):
    """Sign one device out. Revoking your own session logs you out here too."""
    removed = await run_in_threadpool(store.revoke_session, user["user_id"], session_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Session not found")
    await run_in_threadpool(
        audit.log_event, "session_revoked", outcome="success", request=request,
        user_id=user["user_id"], detail={"scope": "one"},
    )
    return {"success": True, "revoked": 1}


@router.delete("/account/sessions")
async def revoke_other_sessions(
    request: Request,
    user: dict = Depends(require_user),
    token: Optional[str] = Depends(bearer_token),
):
    """Sign out everywhere except here, the usual answer to a session you do not
    recognise."""
    count = await run_in_threadpool(store.revoke_other_sessions, user["user_id"], token)
    await run_in_threadpool(
        audit.log_event, "session_revoked", outcome="success", request=request,
        user_id=user["user_id"], detail={"scope": "others", "count": count},
    )
    return {"success": True, "revoked": count}


@router.get("/account/security-events")
async def security_events(
    user: dict = Depends(require_user),
    limit: int = Query(_EVENT_LIMIT, ge=1, le=_EVENT_LIMIT),
):
    """The whitelist of visible event types travels with the response so the
    client does not keep its own copy that drifts."""
    events = await run_in_threadpool(audit.list_events_for_user, user["user_id"], limit)
    return {
        "success": True,
        "count": len(events),
        "events": events,
        "event_types": list(audit.USER_VISIBLE_EVENTS),
        "retention_days": get_settings().security_events_retention_days,
    }


# The export is a copy of your data, not of what the server knows about you as a
# principal, so the credential and the authorization flag stay behind.
_ACCOUNT_SECRETS = ("password_hash", "is_admin")


def _collect_export(user_id: int) -> dict:
    account = store.get_account(user_id) or {}
    return {
        "exported_at": utc_now_iso(),
        "account": {k: v for k, v in account.items() if k not in _ACCOUNT_SECRETS},
        "preferences": store.get_preferences(user_id),
        "watchlists": store.list_favorites(user_id),
        "progress": store.list_progress(user_id),
        "subscriptions": airing_store.list_subscriptions(user_id),
    }


@router.get("/account/export")
@limiter.limit("5/minute")
async def export_account(request: Request, user: dict = Depends(require_user)):
    """Everything this account owns, as one JSON attachment. JSON only: rows of
    five different shapes do not flatten into one CSV without losing something."""
    payload = await run_in_threadpool(_collect_export, user["user_id"])
    stamp = utc_now().strftime("%Y%m%d")
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="crimson-account-{stamp}.json"'
        },
    )


@router.delete("/account")
# Room for a mistyped password: the confirmation is the real gate, and locking
# the owner out of a deliberate action for an hour is the worse failure.
@limiter.limit("5/hour")
async def delete_account(
    request: Request,
    body: DeleteAccountRequest,
    user: dict = Depends(require_user),
):
    """Delete this account and everything cascading from it, irreversibly. The
    audit row is written first and outlives it (see audit.init_db)."""
    await run_in_threadpool(auth.confirm_owner, user, body, request)
    await run_in_threadpool(
        audit.log_event, "account_deleted", outcome="success", request=request,
        user_id=user["user_id"],
        identity=user.get("email") or audit.key_prefix(user.get("public_key")),
        detail={"method": "self_service"},
    )
    removed = await run_in_threadpool(store.delete_account, user["user_id"])
    if not removed:
        raise HTTPException(status_code=404, detail="Account not found")
    return {"success": True, "deleted": True}
