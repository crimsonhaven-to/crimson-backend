"""The chat drawer: status, conversations and the streamed reply.

The reply is NDJSON over POST rather than SSE: EventSource cannot send an
Authorization header or a body, and the client already reads NDJSON for /watch.
"""

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from account_engine.deps import require_user
from core.rate_limit import limiter

from . import conversation, persona
from .access import feature_state, monthly_budget, require_chat_user
from .db import store
from .schemas import ChatRequest

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
    budget = monthly_budget(user, settings)
    if budget > 0 and store.tokens_this_month(user["user_id"]) >= budget:
        raise HTTPException(
            status_code=429,
            detail="You have exhausted this month's audience with me. Ask the operator.",
        )
    return settings, store.get_or_create_conversation(user["user_id"], requested_id)


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
