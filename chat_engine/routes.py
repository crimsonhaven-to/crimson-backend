"""
The chat surface: one streaming endpoint plus small conversation management.

Everything lives under ``/chat`` and is gated three ways, in this order:

  1. a valid session (the site-wide login wall already covers this),
  2. the operator has switched the feature on at all,
  3. this specific account has been granted access.

Deny-by-default is the point: a brand new account can sign in, browse the whole
library and never once be able to spend a token.

Transport
---------
NDJSON over POST, not SSE. EventSource cannot send an Authorization header or a
request body, and this backend already streams NDJSON for ``/watch``, so the
client reuses the reader it already has. Each line is one small JSON object.

Once the response has begun there is no way to send an HTTP error, so everything
inside the generator degrades to an ``error`` line and a clean close. A stuck
drawer is a worse failure than an honest apology in Lumi's voice.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from account_engine.routes import require_user
from core.config import Config
from core.rate_limit import limiter

from . import persona, providers, tools
from .db import ChatStore
from .models import ANTHROPIC, GEMINI, resolve

logger = logging.getLogger("crimson.chat")

router = APIRouter(prefix="/chat", tags=["chat"])
store = ChatStore()

# Long enough for a real question, short enough that nobody pastes a novel into
# the input and turns one message into a large bill.
MAX_MESSAGE_CHARS = 2000


def provider_key(provider: str) -> Optional[str]:
    """The configured key for a provider.

    Keys stay in the environment rather than the settings table so a database
    dump never contains billable credentials. The dashboard is told only whether
    each one is present.
    """
    if provider == ANTHROPIC:
        return Config.ANTHROPIC_API_KEY
    if provider == GEMINI:
        return Config.GEMINI_API_KEY
    return None


def _feature_state(user: Dict) -> Dict:
    """Everything needed to decide whether this viewer may chat, and why not."""
    settings = store.get_settings()
    key_present = bool(provider_key(settings["provider"]))
    granted = bool(user.get("chat_enabled"))
    return {
        "settings": settings,
        "key_present": key_present,
        "granted": granted,
        "available": settings["enabled"] and key_present and granted,
    }


def require_chat_user(user: dict = Depends(require_user)) -> dict:
    """Session plus the chat grant.

    A viewer without the grant gets 403, not 404: the drawer needs to tell them
    to ask the operator rather than pretend the endpoint does not exist. The
    operator-side failures (feature off, key missing) are reported distinctly so
    the admin can tell a misconfiguration from a policy decision.
    """
    state = _feature_state(user)
    if not state["settings"]["enabled"]:
        raise HTTPException(status_code=403, detail="Lumi is not currently awake.")
    if not state["key_present"]:
        raise HTTPException(
            status_code=503, detail="Lumi has no oracle configured. Tell the operator."
        )
    if not state["granted"]:
        raise HTTPException(
            status_code=403, detail="You have not been granted an audience with Lumi."
        )
    return user


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_CHARS)
    conversation_id: Optional[int] = None


@router.get("/status")
async def chat_status(user: dict = Depends(require_user)):
    """Whether this viewer may chat. Safe for any signed-in account to call.

    The drawer polls this once on mount to decide whether to render at all, so it
    must not 403; it answers with flags instead.
    """
    state = await run_in_threadpool(_feature_state, user)
    return {
        "available": state["available"],
        "granted": state["granted"],
        "enabled": state["settings"]["enabled"],
        "configured": state["key_present"],
        "greetings": list(persona.GREETINGS),
    }


@router.get("/conversations")
async def list_conversations(user: dict = Depends(require_chat_user)):
    items = await run_in_threadpool(store.list_conversations, user["user_id"])
    return {"success": True, "conversations": items}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: int, user: dict = Depends(require_chat_user)):
    rows = await run_in_threadpool(
        store.history, conversation_id, user["user_id"], 100
    )
    messages = []
    for row in rows:
        messages.append(
            {
                "role": row["role"],
                "content": row["content"],
                "actions": json.loads(row["actions"]) if row.get("actions") else [],
            }
        )
    return {"success": True, "conversation_id": conversation_id, "messages": messages}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int, user: dict = Depends(require_chat_user)):
    removed = await run_in_threadpool(
        store.delete_conversation, conversation_id, user["user_id"]
    )
    if not removed:
        raise HTTPException(status_code=404, detail="No such conversation")
    return {"success": True}


def _build_context(user: Dict) -> Optional[str]:
    """Per-request grounding block. Cheap enough to always send, never cached.

    One indexed query, no genre profile: the expensive part of a recommendation
    is scoring the catalogue, and Lumi has a tool for that. This exists so she can
    open with something personal without spending a tool call, not so she can
    answer history questions from it.
    """
    from account_engine.routes import store as account_store

    try:
        progress = account_store.list_progress(user["user_id"])[:5]
        recent = [{"title": p.get("title")} for p in progress if p.get("title")]
    except Exception:  # noqa: BLE001 - context is a nicety, never a hard failure
        recent = []
    return persona.build_context_block(
        username=user.get("username"), recent=recent, top_genres=[]
    )


@router.post("")
@limiter.limit("20/minute")
async def chat(request: Request, body: ChatRequest, user: dict = Depends(require_chat_user)):
    """Send one message and stream Lumi's reply as NDJSON.

    Line types, all one JSON object per line:
      ``start``  once, carries the conversation id,
      ``delta``  incremental reply text,
      ``action`` a UI affordance, currently a resolved play link,
      ``done``   terminal, carries the accumulated actions,
      ``error``  terminal, carries a message already phrased for the viewer.
    """
    user_id = user["user_id"]
    settings = await run_in_threadpool(store.get_settings)
    provider = settings["provider"]
    model = resolve(provider, settings["model"])
    api_key = provider_key(provider)

    # Budget is checked before the first call, not during. It bounds the NEXT
    # message rather than the current one, which is what actually stops a
    # runaway loop without cutting a reply in half.
    budget = store.effective_budget(user, settings)
    if budget > 0:
        spent = await run_in_threadpool(store.tokens_this_month, user_id)
        if spent >= budget:
            raise HTTPException(
                status_code=429,
                detail="You have exhausted this month's audience with me. Ask the operator.",
            )

    conversation_id = await run_in_threadpool(
        store.get_or_create_conversation, user_id, body.conversation_id
    )
    message = body.message.strip()

    async def generate() -> AsyncIterator[bytes]:
        def line(obj: Dict) -> bytes:
            return (json.dumps(obj) + "\n").encode("utf-8")

        yield line({"type": "start", "conversation_id": conversation_id})

        collected: List[str] = []
        actions: List[Dict] = []

        try:
            history = await run_in_threadpool(
                store.history, conversation_id, user_id, settings["history_turns"]
            )
            convo: List[providers.Msg] = []
            for row in history:
                if row["role"] == "user":
                    convo.append(providers.user_msg(row["content"]))
                else:
                    convo.append(providers.assistant_msg(row["content"]))

            context = await run_in_threadpool(_build_context, user)
            if context:
                convo.append(providers.user_msg(context))
                convo.append(
                    providers.assistant_msg("Noted. I shall keep it in mind.")
                )
            convo.append(providers.user_msg(message))

            max_iterations = max(1, int(settings["max_tool_iterations"]))
            for _ in range(max_iterations):
                turn = None
                # Deltas are forwarded the instant the provider emits them; the
                # terminal event carries the tool calls and the token counts.
                async for event in providers.stream_turn(
                    provider=provider,
                    api_key=api_key,
                    model=model,
                    system=persona.SYSTEM_PROMPT,
                    messages=convo,
                    tools=tools.TOOL_SCHEMAS,
                ):
                    if event["type"] == "text":
                        collected.append(event["text"])
                        yield line({"type": "delta", "text": event["text"]})
                    elif event["type"] == "turn":
                        turn = event["turn"]

                if turn is None:
                    raise providers.ProviderError(
                        "My oracle went silent mid-sentence. Try again shortly."
                    )

                await run_in_threadpool(
                    store.record_usage,
                    user_id, provider, model.model_id,
                    turn.usage.input_tokens, turn.usage.output_tokens,
                    turn.usage.cached_tokens,
                    model.cost_micros(
                        turn.usage.input_tokens,
                        turn.usage.output_tokens,
                        turn.usage.cached_tokens,
                    ),
                )

                if not turn.wants_tools:
                    break

                convo.append(providers.assistant_msg(turn.text, turn.tool_calls))
                for call in turn.tool_calls:
                    result, action = await tools.dispatch(
                        call.name, call.args, user_id=user_id
                    )
                    convo.append(providers.tool_msg(call, result))
                    if action:
                        actions.append(action)
                        yield line({"type": "action", "action": action})
            else:
                # The loop ran out of iterations with the model still asking for
                # tools. Say so rather than presenting a truncated answer as
                # complete.
                note = " I have gone as far as I care to on that one."
                collected.append(note)
                yield line({"type": "delta", "text": note})

            reply = "".join(collected).strip()
            if reply:
                await run_in_threadpool(
                    store.add_message, conversation_id, user_id, "user", message, None
                )
                await run_in_threadpool(
                    store.add_message,
                    conversation_id, user_id, "assistant", reply, actions or None,
                )
                await run_in_threadpool(
                    store.set_title, conversation_id, user_id, message[:120]
                )

            yield line({"type": "done", "actions": actions})

        except providers.ProviderError as exc:
            yield line({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - see module docstring
            logger.exception("chat stream failed for user %s: %s", user_id, exc)
            yield line(
                {
                    "type": "error",
                    "message": "Something in the crypt has broken. Try again shortly.",
                }
            )

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
