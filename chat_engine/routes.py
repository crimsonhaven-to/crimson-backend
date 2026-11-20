"""The chat drawer: status, conversations and the streamed reply.

The reply is NDJSON over POST rather than SSE: EventSource cannot send an
Authorization header or a body, and the client already reads NDJSON for /watch.
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from account_engine.deps import require_user
from core.public_url import public_base_url
from core.rate_limit import limiter
from music_engine.access import has_music
from music_engine.payloads import track_payload

from . import conversation, dj, persona
from .access import ensure_budget, feature_state, require_chat_user
from .db import store
from .schemas import ChatRequest, DjRequest

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/status")
async def chat_status(user: dict = Depends(require_user)):
    """The drawer asks once on mount whether to render, so this never 403s."""
    state = await asyncio.to_thread(feature_state, user)
    return {
        "available": state["available"],
        "granted": state["granted"],
        "enabled": state["settings"]["enabled"],
        "configured": state["key_present"],
        "greetings": list(persona.GREETINGS),
    }


@router.get("/conversations")
async def list_conversations(user: dict = Depends(require_chat_user)):
    return {
        "success": True,
        "conversations": await asyncio.to_thread(store.list_conversations, user["user_id"]),
    }


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: int, user: dict = Depends(require_chat_user)):
    rows = await asyncio.to_thread(store.history, conversation_id, user["user_id"], 100)
    messages = [
        {
            "role": r["role"],
            "content": r["content"],
            "actions": json.loads(r["actions"]) if r.get("actions") else [],
        }
        for r in rows
    ]
    return {"success": True, "conversation_id": conversation_id, "messages": messages}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int, user: dict = Depends(require_chat_user)):
    if not await asyncio.to_thread(store.delete_conversation, conversation_id, user["user_id"]):
        raise HTTPException(status_code=404, detail="No such conversation")
    return {"success": True}


def _open_conversation(user: dict, requested_id):
    """Settings and conversation id for a new turn, or 429 past the budget. The
    budget bounds the next message rather than cutting the current reply short."""
    settings = store.get_settings()
    ensure_budget(user, settings)
    return settings, store.get_or_create_conversation(user["user_id"], requested_id)


def _dj_settings(user: dict) -> dict:
    settings = store.get_settings()
    ensure_budget(user, settings)
    return settings


@router.post("")
@limiter.limit("20/minute")
async def chat(request: Request, body: ChatRequest, user: dict = Depends(require_chat_user)):
    settings, conversation_id = await asyncio.to_thread(
        _open_conversation, user, body.conversation_id
    )

    async def _lines():
        async for event in conversation.reply(
            user, settings, conversation_id, body.message.strip()
        ):
            yield conversation.ndjson_line(event)

    return StreamingResponse(
        _lines(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/dj")
@limiter.limit("10/minute")
async def dj_set(request: Request, body: DjRequest, user: dict = Depends(require_chat_user)):
    """The next set when Lumi plays DJ. Needs both Lumi and music."""
    if not has_music(user):
        raise HTTPException(status_code=403, detail="Music has not been enabled for your account.")
    settings = await asyncio.to_thread(_dj_settings, user)
    try:
        chosen = await dj.next_set(user, settings, body.played_ids, body.themes)
    except dj.EmptyLibrary:
        raise HTTPException(status_code=404, detail="Nothing in your library is ready to play yet.")
    base = public_base_url(request).rstrip("/")
    return {
        "theme": chosen["theme"],
        "intro": chosen["intro"],
        "voice": chosen["voice"],
        "tracks": [track_payload(t, base) for t in chosen["tracks"]],
    }
