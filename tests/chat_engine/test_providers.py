"""Message shaping for both providers. A wrong role name or an uncoalesced tool
result is accepted by the API and quietly degrades behaviour instead of erroring.
"""

import asyncio

import functools

import json


import httpx


from chat_engine import models, providers, tools


def _sample_history():
    call = providers.ToolCall("toolu_1", "recommend_titles", {"limit": 3})
    return [
        providers.user_msg("what should I watch"),
        providers.assistant_msg("", [call]),
        providers.tool_msg(call, {"recommendations": []}),
        providers.assistant_msg("Try Overlord."),
    ], call


def _run_gemini_stream_over(chunks, monkeypatch=None):
    """Drive ``_gemini_stream`` over a canned SSE body and return the final Turn.

    Still no network: the real client is handed an httpx MockTransport, which
    keeps the production request-shaping and response-parsing code in the test
    rather than reimplementing a fake of it. asyncio.run avoids taking on
    pytest-asyncio for a single generator.
    """
    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)

    def handler(request):
        return httpx.Response(
            200,
            content=body.encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    patched = functools.partial(
        httpx.AsyncClient, transport=httpx.MockTransport(handler)
    )

    async def drive():
        turn = None
        stream = providers._gemini_stream(
            api_key="test-key",
            model=models.resolve("gemini", models.DEFAULT_MODEL["gemini"]),
            system="system",
            messages=[providers.user_msg("hi")],
            tools=tools.TOOL_SCHEMAS,
        )
        async for event in stream:
            if event["type"] == "turn":
                turn = event["turn"]
        return turn

    original = providers.httpx.AsyncClient
    providers.httpx.AsyncClient = patched
    try:
        return asyncio.run(drive())
    finally:
        providers.httpx.AsyncClient = original


def test_anthropic_shaping_batches_tool_results_into_one_user_message():
    """Splitting tool results across messages trains the model out of parallel
    calls, so the coalescing is load-bearing rather than cosmetic."""
    call = providers.ToolCall("toolu_1", "search_catalogue", {"query": "a"})
    call2 = providers.ToolCall("toolu_2", "search_catalogue", {"query": "b"})
    history = [
        providers.user_msg("find both"),
        providers.assistant_msg("", [call, call2]),
        providers.tool_msg(call, {"results": []}),
        providers.tool_msg(call2, {"results": []}),
    ]
    shaped = providers._anthropic_messages(history)
    tool_result_messages = [
        m for m in shaped
        if m["role"] == "user"
        and isinstance(m["content"], list)
        and m["content"][0]["type"] == "tool_result"
    ]
    assert len(tool_result_messages) == 1
    assert len(tool_result_messages[0]["content"]) == 2


def test_anthropic_shaping_roundtrips_a_tool_call():
    history, call = _sample_history()
    shaped = providers._anthropic_messages(history)
    assistant = next(m for m in shaped if m["role"] == "assistant")
    block = next(b for b in assistant["content"] if b["type"] == "tool_use")
    assert block["id"] == call.call_id
    assert block["name"] == "recommend_titles"
    assert block["input"] == {"limit": 3}


def test_anthropic_shaping_serialises_tool_results_as_json():
    history, call = _sample_history()
    shaped = providers._anthropic_messages(history)
    result_block = next(
        b for m in shaped if isinstance(m["content"], list)
        for b in m["content"] if b.get("type") == "tool_result"
    )
    assert result_block["tool_use_id"] == call.call_id
    assert json.loads(result_block["content"]) == {"recommendations": []}


def test_gemini_shaping_uses_the_model_role_and_function_parts():
    history, _ = _sample_history()
    contents = providers._gemini_contents(history)
    roles = [c["role"] for c in contents]
    # Gemini calls the assistant "model" and carries tool results on a user turn.
    assert "model" in roles
    assert "assistant" not in roles
    fn_call = next(
        p for c in contents for p in c["parts"] if "functionCall" in p
    )["functionCall"]
    assert fn_call["name"] == "recommend_titles"
    fn_resp = next(
        p for c in contents for p in c["parts"] if "functionResponse" in p
    )["functionResponse"]
    assert fn_resp["name"] == "recommend_titles"


def test_gemini_echoes_thought_signatures_back_on_replayed_parts():
    """Gemini 3 rejects (400) a replayed functionCall part whose thoughtSignature
    is missing, which breaks the second leg of every tool-using turn. The value is
    opaque and must come back byte-identical."""
    call = providers.ToolCall(
        "gemini-1", "recommend_titles", {"limit": 3}, "SIG_FN"
    )
    contents = providers._gemini_contents(
        [
            providers.user_msg("Something to watch?"),
            providers.assistant_msg("Let me look.", [call], "SIG_TEXT"),
            providers.tool_msg(call, {"recommendations": []}),
        ]
    )
    model_turn = next(c for c in contents if c["role"] == "model")
    fn_part = next(p for p in model_turn["parts"] if "functionCall" in p)
    assert fn_part["thoughtSignature"] == "SIG_FN"
    text_part = next(p for p in model_turn["parts"] if "text" in p)
    assert text_part["thoughtSignature"] == "SIG_TEXT"


def test_gemini_omits_thought_signature_when_there_is_none():
    """History rebuilt from the database has no signatures, and neither does
    anything Anthropic produced. The key must be absent rather than null, since
    an explicit null is a malformed part."""
    call = providers.ToolCall("toolu_1", "recommend_titles", {"limit": 3})
    contents = providers._gemini_contents(
        [
            providers.user_msg("hi"),
            providers.assistant_msg("Try Overlord.", [call]),
        ]
    )
    for content in contents:
        for part in content["parts"]:
            assert "thoughtSignature" not in part


def test_gemini_stream_captures_signatures_per_part():
    """The signature is read per streamed part, because the stream is flattened
    into one text string plus a list of calls and the association is lost after."""
    chunk = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Thinking.", "thoughtSignature": "SIG_TEXT"},
                        {
                            "functionCall": {
                                "name": "recommend_titles",
                                "args": {"limit": 3},
                            },
                            "thoughtSignature": "SIG_FN",
                        },
                    ]
                }
            }
        ]
    }
    turn = _run_gemini_stream_over([chunk])
    assert turn.signature == "SIG_TEXT"
    assert [c.signature for c in turn.tool_calls] == ["SIG_FN"]
    assert turn.tool_calls[0].name == "recommend_titles"


def test_gemini_schema_uppercases_types_only():
    """Gemini's `type` is a proto enum, so lower case is rejected. Everything
    else in the schema must survive untouched."""
    converted = providers._gemini_schema(tools.TOOL_SCHEMAS[0]["input_schema"])
    assert converted["type"] == "OBJECT"

    search = next(t for t in tools.TOOL_SCHEMAS if t["name"] == "search_catalogue")
    converted = providers._gemini_schema(search["input_schema"])
    assert converted["properties"]["query"]["type"] == "STRING"
    # enum values are data, not types, and must keep their case.
    assert converted["properties"]["kind"]["enum"] == ["anime", "show", "movie"]
    assert converted["properties"]["query"]["description"]
    assert converted["required"] == ["query"]
