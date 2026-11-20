"""Reading and sending messages, and the unread count the navigation polls."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from account_engine.deps import require_user
from core.rate_limit import limiter

from . import attachments
from .db import store
from .deps import require_member
from .payloads import message_payload
from .room_routes import room_or_404
from .schemas import MessageIn, ReadIn

router = APIRouter(prefix="/messages", tags=["messages"])

PAGE = 50


@router.get("/unread")
def unread(user: dict = Depends(require_user)):
    """Plain require_user, so the badge can poll before a handle exists."""
    if not user.get("handle"):
        return {"count": 0}
    return {"count": store.unread_total(user["user_id"])}


@router.get("/rooms/{room_id}/messages")
def list_messages(
    room_id: int,
    after: Optional[int] = Query(None, ge=0, description="Only messages newer than this id"),
    before: Optional[int] = Query(None, ge=1, description="The page older than this id"),
    limit: int = Query(PAGE, ge=1, le=100),
    user: dict = Depends(require_member),
):
    room_or_404(user["user_id"], room_id)
    rows = store.messages(room_id, user["user_id"], after=after, before=before, limit=limit)
    return {"messages": [message_payload(r) for r in rows]}


def _refuse_blocked_direct(user: dict, room: dict) -> None:
    others = [m for m in room["members"] if m["user_id"] != user["user_id"]]
    if room["kind"] != "direct" or not others:
        return
    other = others[0]
    if store.has_blocked(user["user_id"], other["user_id"]):
        raise HTTPException(status_code=403, detail=f"You blocked @{other['handle']}")
    if store.has_blocked(other["user_id"], user["user_id"]):
        raise HTTPException(
            status_code=403, detail=f"@{other['handle']} is not accepting your messages"
        )


@router.post("/rooms/{room_id}/messages")
@limiter.limit("30/minute")
def send_message(
    request: Request, room_id: int, body: MessageIn, user: dict = Depends(require_member)
):
    room = room_or_404(user["user_id"], room_id)
    _refuse_blocked_direct(user, room)
    text = body.body.strip()
    attachment = None
    if body.attachment is not None:
        try:
            attachment = attachments.snapshot(body.attachment)
        except attachments.UnknownSong:
            raise HTTPException(status_code=400, detail="That song is no longer in the library")
    if not text and attachment is None:
        raise HTTPException(status_code=400, detail="A message needs some text or a card")
    row = store.send(room_id, user["user_id"], text, attachment)
    return message_payload({
        **row,
        "body": text,
        "attachment": attachment,
        "mine": True,
        "sender_handle": user["handle"],
        "sender_username": user.get("username"),
    })


@router.post("/rooms/{room_id}/read")
def mark_read(room_id: int, body: ReadIn, user: dict = Depends(require_member)):
    room_or_404(user["user_id"], room_id)
    store.mark_read(room_id, user["user_id"], body.last_id)
    return {"success": True}
