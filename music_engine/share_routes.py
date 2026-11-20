"""Song links a member sends to a friend. Minting one needs music and the song in
your own playlists; opening one needs only a session, so a friend without the
music grant can still hear the one song they were sent."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request

from account_engine.deps import require_user
from core.config import Settings, get_settings
from core.public_url import public_base_url

from . import links
from .access import require_music_user
from .db import STATUS_READY, store
from .payloads import track_payload

router = APIRouter(prefix="/music", tags=["music"])


def _share_url(settings: Settings, token: str) -> str:
    return f"{settings.frontend_base_url.rstrip('/')}/music/song/{token}"


@router.post("/tracks/{track_id}/share")
async def share_track(
    track_id: int,
    user: dict = Depends(require_music_user),
    settings: Settings = Depends(get_settings),
):
    """The link points at the website even from the desktop app, whose own origin
    a friend could not open."""
    if not await asyncio.to_thread(store.user_has_track, user["user_id"], track_id):
        raise HTTPException(status_code=404, detail="Track not found")
    token = links.share_token(track_id)
    return {"token": token, "url": _share_url(settings, token)}


@router.get("/shared/{token}")
async def shared_track(
    token: str,
    request: Request,
    _user: dict = Depends(require_user),
    settings: Settings = Depends(get_settings),
):
    track_id = links.shared_track_id(token)
    track = await asyncio.to_thread(store.get_track, track_id) if track_id else None
    if not track or track["status"] != STATUS_READY:
        raise HTTPException(status_code=404, detail="This song is no longer in the library.")
    return {
        "track": track_payload(track, public_base_url(request).rstrip("/")),
        "url": _share_url(settings, token),
    }
