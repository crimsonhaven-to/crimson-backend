"""One chat turn, streamed as events.

The model may call tools several times before it answers; each round's usage is
recorded as it happens. Once the response has begun no HTTP error can be sent,
so any failure becomes an ``error`` event and a clean close: a stuck drawer is
worse than an honest apology in Lumi's voice.
"""

import asyncio
import json
import logging
from typing import AsyncIterator, Dict, List, Optional

from account_engine.db import store as account_store

from . import persona, providers, tools
from .access import provider_key
from .db import store
from .models import resolve

logger = logging.getLogger("crimson.chat")

_OUT_OF_ROUNDS = " I have gone as far as I care to on that one."


def _context_block(user: Dict) -> Optional[str]:
    """A personal opener without spending a tool call: the last few titles, one
    indexed query. Recommendations have their own tool."""
    try:
        recent = [
            {"title": p["title"]}
            for p in account_store.list_progress(user["user_id"])[:5]
            if p.get("title")
        ]
    except Exception:
        recent = []
    return persona.build_context_block(username=user.get("username"), recent=recent)


async def _conversation(user: Dict, conversation_id: int, message: str, history_turns: int) -> List:
    rows = await asyncio.to_thread(store.history, conversation_id, user["user_id"], history_turns)
    convo = [
        providers.user_msg(r["content"])
        if r["role"] == "user"
        else providers.assistant_msg(r["content"])
        for r in rows
    ]
    context = await asyncio.to_thread(_context_block, user)
    if context:
        convo += [
            providers.user_msg(context),
            providers.assistant_msg("Noted. I shall keep it in mind."),
        ]
    convo.append(providers.user_msg(message))
    return convo


async def reply(
    user: Dict, settings: Dict, conversation_id: int, message: str
) -> AsyncIterator[Dict]:
    """``start``, then ``delta`` text and ``action`` affordances as they come,
    then ``done`` with every action, or ``error`` with a message for the viewer."""
    user_id = user["user_id"]
    provider = settings["provider"]
    model = resolve(provider, settings["model"])
    yield {"type": "start", "conversation_id": conversation_id}

    collected: List[str] = []
    actions: List[Dict] = []
    try:
        convo = await _conversation(user, conversation_id, message, settings["history_turns"])
        for _ in range(max(1, int(settings["max_tool_iterations"]))):
            turn = None
            async for event in providers.stream_turn(
                provider=provider,
                api_key=provider_key(provider),
                model=model,
                system=persona.SYSTEM_PROMPT,
                messages=convo,
                tools=tools.TOOL_SCHEMAS,
            ):
                if event["type"] == "text":
                    collected.append(event["text"])
                    yield {"type": "delta", "text": event["text"]}
                elif event["type"] == "turn":
                    turn = event["turn"]
            if turn is None:
                raise providers.ProviderError(
                    "My oracle went silent mid-sentence. Try again shortly."
                )

            usage = turn.usage
            await asyncio.to_thread(
                store.record_usage,
                user_id,
                provider,
                model.model_id,
                usage.input_tokens,
                usage.output_tokens,
                usage.cached_tokens,
                model.cost_micros(usage.input_tokens, usage.output_tokens, usage.cached_tokens),
            )
            if not turn.tool_calls:
                break
            # The signature must ride along: Gemini rejects a replayed turn whose
            # parts lost the thought signatures it minted.
            convo.append(providers.assistant_msg(turn.text, turn.tool_calls, turn.signature))
            for call in turn.tool_calls:
                result, action = await tools.dispatch(call.name, call.args, user_id=user_id)
                convo.append(providers.tool_msg(call, result))
                if action:
                    actions.append(action)
                    yield {"type": "action", "action": action}
        else:
            # Out of rounds with the model still asking for tools: say so rather
            # than present a truncated answer as complete.
            collected.append(_OUT_OF_ROUNDS)
            yield {"type": "delta", "text": _OUT_OF_ROUNDS}

        answer = "".join(collected).strip()
        if answer:
            await asyncio.to_thread(
                _save_exchange, conversation_id, user_id, message, answer, actions
            )
        yield {"type": "done", "actions": actions}
    except providers.ProviderError as exc:
        yield {"type": "error", "message": str(exc)}
    except Exception as exc:
        logger.exception("chat stream failed for user %s: %s", user_id, exc)
        yield {"type": "error", "message": "Something in the crypt has broken. Try again shortly."}


def _save_exchange(
    conversation_id: int, user_id: int, message: str, answer: str, actions: List[Dict]
) -> None:
    store.add_message(conversation_id, user_id, "user", message, None)
    store.add_message(conversation_id, user_id, "assistant", answer, actions or None)
    store.set_title(conversation_id, user_id, message)


def ndjson_line(event: Dict) -> bytes:
    return (json.dumps(event) + "\n").encode("utf-8")
