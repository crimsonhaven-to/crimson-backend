"""Starting chats, naming and joining groups, and leaving them."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request

from core.rate_limit import limiter

from . import handles
from .db import store
from .deps import require_member
from .payloads import room_payload
from .schemas import MAX_GROUP_MEMBERS, AddMembersIn, DirectIn, GroupIn, RenameIn

router = APIRouter(prefix="/messages", tags=["messages"])


def room_or_404(user_id: int, room_id: int) -> dict:
    """Someone else's room is indistinguishable from one that does not exist."""
    rows = store.rooms(user_id, room_id)
    if not rows:
        raise HTTPException(status_code=404, detail="Chat not found")
    return rows[0]


def _group_or_400(user_id: int, room_id: int) -> dict:
    room = room_or_404(user_id, room_id)
    if room["kind"] != "group":
        raise HTTPException(status_code=400, detail="Only a group can do that")
    return room


def _refuse_blockers(user: dict, others: List[dict]) -> None:
    blockers = set(store.blocked_by_any(user["user_id"], [o["user_id"] for o in others]))
    for other in others:
        if other["user_id"] in blockers:
            raise HTTPException(
                status_code=403, detail=f"@{other['handle']} is not accepting your messages"
            )


def _group_name(raw: str) -> str:
    name = raw.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A group needs a name")
    return name


@router.get("/rooms")
def list_rooms(user: dict = Depends(require_member)):
    return {"rooms": [room_payload(r) for r in store.rooms(user["user_id"])]}


@router.get("/rooms/{room_id}")
def get_room(room_id: int, user: dict = Depends(require_member)):
    return room_payload(room_or_404(user["user_id"], room_id))


@router.post("/direct")
@limiter.limit("20/minute")
def open_direct(request: Request, body: DirectIn, user: dict = Depends(require_member)):
    """The same two people always land in the same chat."""
    other = handles.resolve_one(body.handle)
    if other["user_id"] == user["user_id"]:
        raise HTTPException(status_code=400, detail="That's you")
    if store.has_blocked(user["user_id"], other["user_id"]):
        raise HTTPException(status_code=403, detail=f"You blocked @{other['handle']}")
    _refuse_blockers(user, [other])
    room_id = store.direct_room(user["user_id"], other["user_id"])
    return room_payload(room_or_404(user["user_id"], room_id))


@router.post("/groups")
@limiter.limit("10/minute")
def create_group(request: Request, body: GroupIn, user: dict = Depends(require_member)):
    name = _group_name(body.name)
    others = [m for m in handles.resolve(body.handles) if m["user_id"] != user["user_id"]]
    if not others:
        raise HTTPException(status_code=400, detail="Add at least one other member")
    _refuse_blockers(user, others)
    room_id = store.create_group(name, [user["user_id"]] + [o["user_id"] for o in others])
    return room_payload(room_or_404(user["user_id"], room_id))


@router.patch("/rooms/{room_id}")
@limiter.limit("20/minute")
def rename_group(
    request: Request, room_id: int, body: RenameIn, user: dict = Depends(require_member)
):
    _group_or_400(user["user_id"], room_id)
    store.rename(room_id, _group_name(body.name))
    return room_payload(room_or_404(user["user_id"], room_id))


@router.post("/rooms/{room_id}/members")
@limiter.limit("20/minute")
def add_members(
    request: Request, room_id: int, body: AddMembersIn, user: dict = Depends(require_member)
):
    """Any member of a group may add people, as in most group chats."""
    room = _group_or_400(user["user_id"], room_id)
    present = {m["handle"] for m in room["members"]}
    new = [m for m in handles.resolve(body.handles) if m["handle"] not in present]
    if len(present) + len(new) > MAX_GROUP_MEMBERS:
        raise HTTPException(
            status_code=400, detail=f"A group holds at most {MAX_GROUP_MEMBERS} members"
        )
    _refuse_blockers(user, new)
    if new:
        store.add_members(room_id, [m["user_id"] for m in new])
    return room_payload(room_or_404(user["user_id"], room_id))


@router.delete("/rooms/{room_id}/membership")
def leave_group(room_id: int, user: dict = Depends(require_member)):
    """A direct chat is never left (blocking ends it), so the other person keeps
    their history and the pair never ends up with two chats."""
    _group_or_400(user["user_id"], room_id)
    store.leave(room_id, user["user_id"])
    return {"success": True}
