"""
What an account holder can see and do about their own account's security.

The backend has kept a full security ledger and a session table since the
beginning and showed the user none of it: ``/security/events`` and
``/users/{id}/revoke-sessions`` are admin-only (see admin_routes). This module is
the same information, narrowed to the caller:

    GET    /account/sessions            where you are signed in
    DELETE /account/sessions/{id}       sign out one device
    DELETE /account/sessions            sign out everywhere else
    GET    /account/security-events     what has happened to your account
    GET    /account/export              every row this account owns
    DELETE /account                     delete it, for real

Two rules run through all of it. A session is addressed by a one-way derived id,
never by ``token_hash`` (account_engine.db explains why). Events are filtered on
``user_id`` alone and carry neither ``detail`` nor ``identity``.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import audit, passwords
from .routes import require_user, store, _verify_signed_challenge
from core.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["account-security"])

# Enough to cover "what happened while I was away" without paging a table that
# has no per-user cursor.
_EVENT_LIMIT = 100


def bearer_token(authorization: Optional[str] = Header(None)) -> Optional[str]:
    """The caller's raw session token, for the two places that must know which
    session is theirs. Returns None rather than raising: ``require_user`` runs
    alongside it and owns rejecting an absent or bad token."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()


# --- sessions ---------------------------------------------------------------
@router.get("/account/sessions")
async def list_sessions(
    user: dict = Depends(require_user),
    token: Optional[str] = Depends(bearer_token),
):
    """Live sessions for this account, newest first, with the caller's flagged.

    ``user_agent`` and ``ip`` are null for sessions created before they were
    recorded. Those render as an unknown device rather than being dropped: a
    session you cannot place is the one most worth seeing."""
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


# --- the ledger -------------------------------------------------------------
@router.get("/account/security-events")
async def security_events(
    user: dict = Depends(require_user),
    limit: int = Query(_EVENT_LIMIT, ge=1, le=_EVENT_LIMIT),
):
    """Recent security events belonging to this account.

    The visible event types are a whitelist (audit.USER_VISIBLE_EVENTS); the list
    travels with the response so the client can label what it renders instead of
    keeping its own copy that drifts."""
    events = await run_in_threadpool(audit.list_events_for_user, user["user_id"], limit)
    return {
        "success": True,
        "count": len(events),
        "events": events,
        "event_types": list(audit.USER_VISIBLE_EVENTS),
        "retention_days": audit.RETENTION_DAYS,
    }


# --- export -----------------------------------------------------------------
# The account row holds a credential and an authorization flag. The export is a
# copy of your data, not of what the server knows about you as a principal, so
# these never travel with it.
_ACCOUNT_SECRETS = ("password_hash", "is_admin")


def _collect_export(user_id: int) -> dict:
    account = store.get_account(user_id) or {}
    # Imported here rather than at module scope: notify_engine imports this
    # package's routes for require_user, so a top-level import closes the cycle.
    from notify_engine.db import store as airing_store

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {k: v for k, v in account.items() if k not in _ACCOUNT_SECRETS},
        "preferences": store.get_preferences(user_id),
        "watchlists": store.list_favorites(user_id),
        "progress": store.list_progress(user_id),
        "subscriptions": airing_store.list_subscriptions(user_id),
    }


@router.get("/account/export")
@limiter.limit("5/minute")
async def export_account(request: Request, user: dict = Depends(require_user)):
    """Everything this account owns, as one JSON attachment.

    ``/account/favorites/export`` already does the watchlists in a spreadsheet
    shape. This is the whole account instead, and stays JSON only: rows of five
    different shapes do not flatten into one CSV without losing something."""
    payload = await run_in_threadpool(_collect_export, user["user_id"])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="crimson-account-{stamp}.json"'
        },
    )


# --- deletion ---------------------------------------------------------------
class DeleteAccountRequest(BaseModel):
    """Proof that the person asking is the account holder, not a stolen token.

    An email account confirms with its password. A mnemonic account signs a
    one-time challenge, exactly as it does to log in, so nothing new has to be
    invented for an identity that has no password to re-enter."""
    password: Optional[str] = Field(None, max_length=passwords.MAX_PASSWORD_LENGTH)
    challenge: Optional[str] = None
    signature: Optional[str] = None


def _confirm_owner(user: dict, body: DeleteAccountRequest, request: Request) -> None:
    """Re-prove ownership, or raise. A bearer token alone is not enough here: it
    is the one credential an attacker can hold without being the owner, and this
    is the one action that cannot be undone."""
    stored_hash = user.get("password_hash")
    if stored_hash:
        if not body.password or not passwords.verify_password(body.password, stored_hash):
            audit.log_event(
                "account_delete_failed", outcome="failure", request=request,
                user_id=user["user_id"], detail={"reason": "bad_password"},
            )
            raise HTTPException(status_code=401, detail="Password is incorrect")
        return

    public_key = user.get("public_key")
    if not public_key:
        raise HTTPException(
            status_code=400,
            detail="This account has no password or key to confirm with; contact an admin",
        )
    if not body.challenge or not body.signature:
        raise HTTPException(
            status_code=400,
            detail="Sign a challenge from /auth/challenge to confirm deletion",
        )
    # Raises 401 on any failure and records it, the same as a failed login.
    _verify_signed_challenge(
        public_key, body.challenge, body.signature, request, "delete_account"
    )


@router.delete("/account")
@limiter.limit("3/hour")
async def delete_account(
    request: Request,
    body: DeleteAccountRequest,
    user: dict = Depends(require_user),
):
    """Delete this account and everything cascading from it, irreversibly.

    The audit row is written before the delete and deliberately outlives it:
    ``security_events.user_id`` has no foreign key precisely so the trail
    survives the account it describes (see audit.init_db). Favorites, progress
    and sessions do cascade, which is also correct."""
    await run_in_threadpool(_confirm_owner, user, body, request)
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
