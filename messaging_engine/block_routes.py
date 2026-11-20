"""Looking a member up by handle, and blocking the ones you never want to hear
from. A block stops their direct messages and stops them adding you to groups;
it does not remove you from a group you share, which you can leave."""

from fastapi import APIRouter, Depends, HTTPException, Request

from core.rate_limit import limiter

from . import handles
from .db import store
from .deps import require_member
from .payloads import member_payload

router = APIRouter(prefix="/messages", tags=["messages"])


@router.get("/members/{handle}")
@limiter.limit("30/minute")
def find_member(request: Request, handle: str, user: dict = Depends(require_member)):
    """Rate limited, since every answer says whether a handle exists."""
    member = handles.resolve_one(handle)
    return {
        **member_payload(member),
        "blocked": store.has_blocked(user["user_id"], member["user_id"]),
    }


@router.get("/blocks")
def list_blocks(user: dict = Depends(require_member)):
    return {"blocks": [member_payload(b) for b in store.blocks(user["user_id"])]}


@router.put("/blocks/{handle}")
def block(handle: str, user: dict = Depends(require_member)):
    member = handles.resolve_one(handle)
    if member["user_id"] == user["user_id"]:
        raise HTTPException(status_code=400, detail="That's you")
    store.block(user["user_id"], member["user_id"])
    return {"success": True}


@router.delete("/blocks/{handle}")
def unblock(handle: str, user: dict = Depends(require_member)):
    store.unblock(user["user_id"], handles.resolve_one(handle)["user_id"])
    return {"success": True}
