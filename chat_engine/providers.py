"""
Anthropic and Gemini behind one streaming, tool-calling interface.

The rest of the engine speaks one neutral message format and never learns which
vendor answered. That is what makes the provider an operator dropdown rather than
a redeploy.

Anthropic goes through the official SDK, which handles retries, streaming state
and typed errors. Gemini goes through plain httpx against the REST endpoint,
because the surface needed here is one POST with an SSE body and the alternative
is a second heavy SDK in a deliberately pinned image.

Both paths are async generators rather than callback-driven: a callback cannot
yield out of the caller's frame, so it would buffer the whole reply and hand it
over at the end, which is precisely the streaming this feature exists to provide.
Text arrives as ``text`` events; tool calls are collected and delivered whole on
the terminal ``turn`` event, since a half-decoded argument object helps nobody.

Thought signatures
------------------
Gemini 3 stamps an opaque ``thoughtSignature`` on the part its reasoning produced
and rejects the next request with a 400 if that part returns without it. Tool use
is where this bites: the first call succeeds, then the second leg of the loop
replays the turn as history and fails, so the feature looks broken only once a
tool is involved. The value therefore rides the neutral format and is echoed back
verbatim. It is provider bookkeeping; nothing else may read it.

Signatures live only as long as the request that minted them, so history reloaded
from the database has none. That is fine: the requirement applies to a turn
replayed inside one tool loop, and Anthropic has no equivalent concept.

Prompt caching is requested on the Anthropic path by marking the last system
block. The persona plus tool schemas come to roughly 1.8k tokens, clearing the
minimum on every model but Haiku 4.5, where the marker is accepted and silently
does nothing. Gemini's implicit caching needs no flag. This is why
``SYSTEM_PROMPT`` is frozen: a timestamp there would turn every cache read into a
cache write.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Dict, List, Optional

import httpx

from .models import ANTHROPIC, GEMINI, ChatModel

# Optional, like prometheus-client: a stripped build still boots and serves the
# whole API, reporting only this provider as unavailable.
try:
    import anthropic
except ImportError:  # pragma: no cover - only on a stripped build
    # Rebinding a module name to None is the pattern an optional import needs and
    # the one thing mypy cannot express, so it is silenced rather than worked
    # around with a second sentinel name.
    anthropic = None  # type: ignore[assignment]

logger = logging.getLogger("crimson.chat.providers")

ANTHROPIC_SDK_AVAILABLE = anthropic is not None

# The persona asks for two or three sentences, so this is a ceiling rather than a
# target. It also bounds the cost of a model that decides to monologue.
MAX_OUTPUT_TOKENS = 2048

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"

# Events yielded by the streaming generators:
#   {"type": "text", "text": str}   incremental reply text
#   {"type": "turn", "turn": Turn}  terminal, once, with tool calls and usage
Event = Dict


class ProviderError(RuntimeError):
    """A provider call failed in a way the viewer should be told about."""


class ToolCall:
    """One requested tool invocation.

    ``signature`` is opaque provider bookkeeping, echoed back verbatim when this
    call is replayed as history. Gemini 3 hard-errors with a 400 if the follow-up
    request has dropped it, which fails every tool-using conversation on the
    second leg of the loop. Anthropic leaves it None, and nothing outside the
    provider that minted it may interpret the value.
    """

    __slots__ = ("call_id", "name", "args", "signature")

    def __init__(
        self, call_id: str, name: str, args: Dict, signature: Optional[str] = None
    ):
        self.call_id = call_id
        self.name = name
        self.args = args or {}
        self.signature = signature


class Usage:
    __slots__ = ("input_tokens", "output_tokens", "cached_tokens")

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens


class Turn:
    """One round trip: what it said, what it wants to call, what it cost."""

    __slots__ = ("text", "tool_calls", "usage", "signature")

    def __init__(
        self,
        text: str,
        tool_calls: List[ToolCall],
        usage: Usage,
        signature: Optional[str] = None,
    ):
        self.text = text
        self.tool_calls = tool_calls
        self.usage = usage
        # Arrived on a text part rather than a functionCall one. Same contract as
        # ToolCall.signature.
        self.signature = signature

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


# --- neutral message format -------------------------------------------------
# A conversation is a list of these dicts:
#   {"role": "user",      "text": str}
#   {"role": "assistant", "text": str, "tool_calls": [ToolCall]}
#   {"role": "tool",      "call_id": str, "name": str, "result": dict}
Msg = Dict


def user_msg(text: str) -> Msg:
    return {"role": "user", "text": text}


def assistant_msg(
    text: str,
    tool_calls: Optional[List[ToolCall]] = None,
    signature: Optional[str] = None,
) -> Msg:
    return {
        "role": "assistant",
        "text": text,
        "tool_calls": tool_calls or [],
        "signature": signature,
    }


def tool_msg(call: ToolCall, result: Dict) -> Msg:
    return {"role": "tool", "call_id": call.call_id, "name": call.name, "result": result}


# --- Anthropic --------------------------------------------------------------
def _anthropic_messages(messages: List[Msg]) -> List[Dict]:
    """Neutral history to Anthropic content blocks.

    Tool results must be batched: several blocks from one assistant turn have to
    arrive in a single user message. Splitting them is accepted by the API but
    trains the model out of making parallel calls, so they are coalesced here.
    """
    out: List[Dict] = []
    pending_results: List[Dict] = []

    def flush_results():
        if pending_results:
            out.append({"role": "user", "content": list(pending_results)})
            pending_results.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            pending_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": msg["call_id"],
                    "content": json.dumps(msg.get("result") or {}),
                }
            )
            continue

        flush_results()
        if role == "user":
            out.append({"role": "user", "content": msg.get("text") or ""})
        elif role == "assistant":
            blocks: List[Dict] = []
            if msg.get("text"):
                blocks.append({"type": "text", "text": msg["text"]})
            for call in msg.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.call_id,
                        "name": call.name,
                        "input": call.args,
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})

    flush_results()
    return out


async def _anthropic_stream(
    *,
    api_key: str,
    model: ChatModel,
    system: str,
    messages: List[Msg],
    tools: List[Dict],
) -> AsyncIterator[Event]:
    if anthropic is None:
        raise ProviderError(
            "This build has no Anthropic client installed. Switch provider to "
            "Gemini, or install the anthropic package."
        )
    client = anthropic.AsyncAnthropic(api_key=api_key)

    request: Dict = {
        "model": model.model_id,
        "max_tokens": MAX_OUTPUT_TOKENS,
        # A list rather than a bare string, so the cache breakpoint can attach.
        # Tools render before system, so this one marker covers both.
        "system": [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": _anthropic_messages(messages),
        "tools": [
            {
                "name": t["name"],
                "description": t["description"],
                "input_schema": t["input_schema"],
            }
            for t in tools
        ],
    }

    # The current models reject sampling parameters outright, so the persona does
    # the steering and depth rides on effort, which older Haiku will not accept.
    if model.supports_thinking:
        request["thinking"] = {"type": "adaptive"}
    if model.supports_effort:
        # Chat wants latency over deliberation, and the tool set is small enough
        # that low effort still routes correctly.
        request["output_config"] = {"effort": "low"}

    text_parts: List[str] = []
    try:
        async with client.messages.stream(**request) as stream:
            async for chunk in stream.text_stream:
                text_parts.append(chunk)
                yield {"type": "text", "text": chunk}
            final = await stream.get_final_message()
    except anthropic.APIStatusError as exc:
        logger.warning("anthropic call failed: %s %s", exc.status_code, exc.message)
        raise ProviderError(_friendly_error(exc.status_code)) from exc
    except anthropic.APIConnectionError as exc:
        raise ProviderError("I could not reach my oracle. Try again shortly.") from exc

    calls = [
        ToolCall(block.id, block.name, block.input)
        for block in final.content
        if block.type == "tool_use"
    ]
    usage = Usage(
        input_tokens=final.usage.input_tokens or 0,
        output_tokens=final.usage.output_tokens or 0,
        # Writes bill above the base rate and reads below it. Only reads are
        # tracked: a write happens once per prefix change, and counting it as a
        # normal input token over-reports rather than under.
        cached_tokens=getattr(final.usage, "cache_read_input_tokens", 0) or 0,
    )
    yield {"type": "turn", "turn": Turn("".join(text_parts), calls, usage)}


def _friendly_error(status: int) -> str:
    if status == 401:
        return "My key to the oracle was refused. The operator must check it."
    if status == 429:
        return "Too many questions at once. Wait a moment, then ask again."
    if status >= 500:
        return "The oracle is unwell. This is not my fault. Try again shortly."
    return "That request displeased the oracle. Try phrasing it differently."


# --- Gemini -----------------------------------------------------------------
# Function declarations take an OpenAPI schema whose `type` is a proto enum, so
# those values must be upper case. The rest of the subset carries over unchanged.
def _gemini_schema(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "type" and isinstance(value, str):
                out[key] = value.upper()
            else:
                out[key] = _gemini_schema(value)
        return out
    if isinstance(node, list):
        return [_gemini_schema(v) for v in node]
    return node


def _gemini_contents(messages: List[Msg]) -> List[Dict]:
    """Neutral history to Gemini ``contents``.

    Gemini calls the assistant role "model" and carries tool results as a
    functionResponse part on a user turn. Consecutive results coalesce, as on the
    Anthropic path.
    """
    out: List[Dict] = []
    pending: List[Dict] = []

    def flush():
        if pending:
            out.append({"role": "user", "parts": list(pending)})
            pending.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            pending.append(
                {
                    "functionResponse": {
                        "name": msg["name"],
                        "response": msg.get("result") or {},
                    }
                }
            )
            continue

        flush()
        if role == "user":
            out.append({"role": "user", "parts": [{"text": msg.get("text") or ""}]})
        elif role == "assistant":
            parts: List[Dict] = []
            if msg.get("text"):
                text_part = {"text": msg["text"]}
                if msg.get("signature"):
                    text_part["thoughtSignature"] = msg["signature"]
                parts.append(text_part)
            for call in msg.get("tool_calls") or []:
                fn_part: Dict = {"functionCall": {"name": call.name, "args": call.args}}
                # Replaying a call without the signature Gemini minted is a 400,
                # not a soft warning. Absent on history rebuilt from the database
                # and on anything Anthropic produced.
                if call.signature:
                    fn_part["thoughtSignature"] = call.signature
                parts.append(fn_part)
            if parts:
                out.append({"role": "model", "parts": parts})

    flush()
    return out


async def _gemini_stream(
    *,
    api_key: str,
    model: ChatModel,
    system: str,
    messages: List[Msg],
    tools: List[Dict],
) -> AsyncIterator[Event]:
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": _gemini_contents(messages),
        "tools": [
            {
                "functionDeclarations": [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": _gemini_schema(t["input_schema"]),
                    }
                    for t in tools
                ]
            }
        ],
        "generationConfig": {"maxOutputTokens": MAX_OUTPUT_TOKENS},
    }

    url = f"{GEMINI_ENDPOINT}/{model.model_id}:streamGenerateContent?alt=sse"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    text_parts: List[str] = []
    calls: List[ToolCall] = []
    usage = Usage()
    call_seq = 0
    text_signature: Optional[str] = None

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    await response.aread()
                    logger.warning(
                        "gemini call failed: %s %s", response.status_code, response.text[:400]
                    )
                    raise ProviderError(_friendly_error(response.status_code))

                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue

                    # Cumulative and repeated per chunk, so the last is the total.
                    meta = chunk.get("usageMetadata") or {}
                    if meta:
                        cached = int(meta.get("cachedContentTokenCount") or 0)
                        usage = Usage(
                            input_tokens=max(0, int(meta.get("promptTokenCount") or 0) - cached),
                            output_tokens=int(meta.get("candidatesTokenCount") or 0),
                            cached_tokens=cached,
                        )

                    for candidate in chunk.get("candidates") or []:
                        for part in (candidate.get("content") or {}).get("parts") or []:
                            # Read per part, because the stream is flattened into
                            # one string plus a list of calls and the association
                            # is lost after that.
                            signature = part.get("thoughtSignature")
                            if "text" in part and part["text"]:
                                text_parts.append(part["text"])
                                # First wins: it belongs to the leading text part,
                                # and the reply replays as one concatenated part.
                                if signature and text_signature is None:
                                    text_signature = signature
                                yield {"type": "text", "text": part["text"]}
                            fn = part.get("functionCall")
                            if fn and fn.get("name"):
                                call_seq += 1
                                # Gemini pairs by name rather than minting ids, so
                                # one is synthesised for the neutral format and
                                # the ledger to key on.
                                calls.append(
                                    ToolCall(
                                        f"gemini-{call_seq}",
                                        fn["name"],
                                        fn.get("args") or {},
                                        signature,
                                    )
                                )
    except httpx.HTTPError as exc:
        raise ProviderError("I could not reach my oracle. Try again shortly.") from exc

    yield {
        "type": "turn",
        "turn": Turn("".join(text_parts), calls, usage, text_signature),
    }


# --- entry point ------------------------------------------------------------
async def stream_turn(
    *,
    provider: str,
    # Optional on purpose, since "not configured" is a normal state. Rejecting it
    # here in Lumi's voice beats making every call site prove it is set.
    api_key: Optional[str],
    model: ChatModel,
    system: str,
    messages: List[Msg],
    tools: List[Dict],
) -> AsyncIterator[Event]:
    """One round trip to whichever provider the operator selected.

    Yields ``text`` events as they arrive and exactly one terminal ``turn``.
    Raises ProviderError for anything the viewer should be told about.
    """
    if not api_key:
        raise ProviderError(
            "My oracle has no key. The operator must configure one before I can speak."
        )
    if provider == ANTHROPIC:
        stream = _anthropic_stream(
            api_key=api_key, model=model, system=system, messages=messages, tools=tools
        )
    elif provider == GEMINI:
        stream = _gemini_stream(
            api_key=api_key, model=model, system=system, messages=messages, tools=tools
        )
    else:
        raise ProviderError(f"Unknown provider: {provider}")

    async for event in stream:
        yield event
