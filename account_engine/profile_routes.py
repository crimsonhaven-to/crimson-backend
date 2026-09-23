"""The signed-in account itself: who it is, its client preferences and its
display name."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from core.rate_limit import limiter

from .db import store
from .deps import require_user
from .schemas import MAX_USERNAME_LENGTH, UsernameIn

router = APIRouter(tags=["account"])

# An open key/value bag, so a new preference is a client-only change.
_MAX_PREFERENCES_BYTES = 4096


@router.get("/account/me")
def account_me(user: dict = Depends(require_user)):
    counts = store.library_counts(user["user_id"])
    return {
        "success": True,
        "user_id": user.get("user_id"),
        "public_key": user.get("public_key"),
        "email": user.get("email"),
        "email_verified": user.get("email_verified"),
        "is_admin": bool(user.get("is_admin")),
        "username": user.get("username"),
        "label": user.get("label"),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
        "favorites_count": counts["favorites"],
        "progress_count": counts["progress"],
        "preferences": store.get_preferences(user["user_id"]),
    }


@router.get("/account/preferences")
def get_preferences(user: dict = Depends(require_user)):
    return {"success": True, "preferences": store.get_preferences(user["user_id"])}


@router.put("/account/preferences")
@limiter.limit("30/minute")
async def put_preferences(request: Request, user: dict = Depends(require_user)):
    """Replace the preferences with the JSON object in the raw body."""
    raw = await request.body()
    if len(raw) > _MAX_PREFERENCES_BYTES:
        raise HTTPException(status_code=413, detail="Preferences payload too large")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Preferences must be a JSON object")
    saved = await run_in_threadpool(store.set_preferences, user["user_id"], data)
    return {"success": True, "preferences": saved}


@router.put("/account/username")
@limiter.limit("20/minute")
def set_username(request: Request, body: UsernameIn, user: dict = Depends(require_user)):
    """A cosmetic, non-unique greeting name. An empty value clears it."""
    name = (body.username or "").strip()
    if len(name) > MAX_USERNAME_LENGTH:
        raise HTTPException(
            status_code=400, detail=f"Name must be at most {MAX_USERNAME_LENGTH} characters"
        )
    store.set_username(user["user_id"], name or None)
    return {"success": True, "username": name or None}
