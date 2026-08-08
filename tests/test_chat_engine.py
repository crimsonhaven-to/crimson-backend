"""Fixture tests for Lumi's chatbot engine (chat_engine/).

Like the rest of the suite these make no network calls and need no database. The
parts worth pinning here are the ones that break silently:

  * the persona's house-style rules, which are a product requirement rather than
    a preference and cannot be caught by any other check,
  * route building, which is the one piece of frontend knowledge the backend
    holds, so a client route rename must fail here rather than in production,
  * cost arithmetic, which is what the dashboard bills against,
  * the message-shape translation for both providers, where a wrong role name or
    an uncoalesced tool result is accepted by the API and quietly degrades
    behaviour instead of erroring.
"""

import json
import re

import pytest

from chat_engine import models, persona, providers, tools


# --- persona ---------------------------------------------------------------
# Em and en dashes are banned in Lumi's output. The rule is stated in the prompt,
# but a prompt that itself contains the character it forbids is both a mixed
# signal to the model and a sign someone edited the file without reading it, so
# the prompt is checked as well as the rule's presence.
EM_DASH = "—"
EN_DASH = "–"


def test_system_prompt_states_the_dash_ban():
    assert "em dash" in persona.SYSTEM_PROMPT.lower()


def test_system_prompt_contains_no_dashes_itself():
    # The ban characters appear exactly where the rule quotes them, and nowhere
    # else. Quoting them is what makes the instruction unambiguous.
    rule_line = next(
        line for line in persona.SYSTEM_PROMPT.splitlines() if "NEVER use em dashes" in line
    )
    body = persona.SYSTEM_PROMPT.replace(rule_line, "")
    assert EM_DASH not in body
    assert EN_DASH not in body


def test_greetings_carry_no_dashes():
    for line in persona.GREETINGS:
        assert EM_DASH not in line and EN_DASH not in line


def test_system_prompt_is_frozen_and_cacheable():
    """It is the cached prefix, so it must not vary between reads.

    Anything interpolated per request would change the prompt bytes on every
    call and silently turn every cache read into a cache write.
    """
    assert persona.SYSTEM_PROMPT == persona.SYSTEM_PROMPT
    assert "{" not in persona.SYSTEM_PROMPT.replace("{}", "")


def test_context_block_is_none_without_facts():
    assert persona.build_context_block(username=None, recent=[], top_genres=[]) is None


def test_context_block_summarises_what_it_has():
    block = persona.build_context_block(
        username="Ramon",
        recent=[{"title": "Overlord"}, {"title": "Frieren"}],
        top_genres=["Fantasy", "Action"],
    )
    assert "Ramon" in block
    assert "Overlord" in block
    assert "Fantasy" in block


# --- routes ----------------------------------------------------------------
# These mirror the paths declared in the client's App.jsx. If a route is renamed
# there, this test is the tripwire.
@pytest.mark.parametrize(
    "kind,kwargs,expected",
    [
        ("anime", {"anilist_id": 108465, "season": 2, "episode": 1}, "/watch/108465/2/1"),
        ("anime", {"anilist_id": 108465}, "/watch/108465/1/1"),
        ("show", {"tmdb_id": 1399, "season": 3, "episode": 9}, "/watch-show/1399/3/9"),
        ("movie", {"tmdb_id": 1014505}, "/watch-movie/1014505"),
    ],
)
def test_build_route(kind, kwargs, expected):
    assert tools.build_route(kind, **kwargs) == expected


@pytest.mark.parametrize(
    "kind,kwargs",
    [
        ("anime", {"tmdb_id": 1399}),   # anime needs an anilist id
        ("show", {"anilist_id": 108465}),  # shows need a tmdb id
        ("movie", {}),
    ],
)
def test_build_route_rejects_mismatched_ids(kind, kwargs):
    assert tools.build_route(kind, **kwargs) is None


def test_build_route_clamps_nonsense_season_and_episode():
    assert tools.build_route("anime", anilist_id=1, season=0, episode=-5) == "/watch/1/1/1"


def test_item_key_matches_the_account_engine_scheme():
    """The watchlist dedup key must match account_engine.routes._favorite_item_key.

    They are separate implementations on purpose (that helper is private to the
    routes module), so the shapes are asserted equal here instead.
    """
    from account_engine.routes import _favorite_item_key

    assert tools._item_key("anime", 108465, None) == _favorite_item_key(None, 108465)
    assert tools._item_key("movie", None, 1014505) == _favorite_item_key(1014505, None, "movie")
    assert tools._item_key("show", None, 1399) == _favorite_item_key(1399, None)


# --- tool schemas ----------------------------------------------------------
def test_every_tool_has_a_handler():
    assert set(tools.TOOL_NAMES) == set(tools._HANDLERS)


def test_tool_schemas_are_well_formed():
    for schema in tools.TOOL_SCHEMAS:
        assert schema["name"] and schema["description"]
        params = schema["input_schema"]
        assert params["type"] == "object"
        # Every property carries a description: it is what the model reads to
        # decide what to put there.
        for prop in params["properties"].values():
            assert prop.get("description")
        # Required entries must actually exist as properties.
        for name in params.get("required", []):
            assert name in params["properties"]


def test_dispatch_of_an_unknown_tool_returns_an_error_not_a_raise():
    import asyncio

    result, action = asyncio.run(tools.dispatch("no_such_tool", {}, user_id=1))
    assert "error" in result
    assert action is None


# --- model catalogue -------------------------------------------------------
def test_no_shutdown_gemini_models_are_offered():
    """Gemini 2.0 is shut down; offering it would be a 404 at request time."""
    for model_id in models.MODELS:
        assert not model_id.startswith("gemini-2.0")


def test_every_provider_default_exists_and_matches_its_provider():
    for provider, model_id in models.DEFAULT_MODEL.items():
        model = models.get_model(model_id)
        assert model is not None
        assert model.provider == provider


def test_resolve_falls_back_when_the_model_belongs_to_the_other_provider():
    """Guards a provider switch that left the other vendor's model id stored."""
    model = models.resolve("gemini", "claude-sonnet-5")
    assert model.provider == "gemini"
    assert model.model_id == models.DEFAULT_MODEL["gemini"]


def test_resolve_keeps_a_valid_pairing():
    assert models.resolve("anthropic", "claude-opus-5").model_id == "claude-opus-5"


def test_cost_arithmetic():
    model = models.get_model("claude-sonnet-5")
    # 1M input at $3 plus 1M output at $15 is $18, or 18,000,000 micros.
    assert model.cost_micros(1_000_000, 1_000_000) == 18_000_000
    # Cached reads bill at the reduced rate, not the full input rate.
    assert model.cost_micros(0, 0, 1_000_000) == 300_000
    assert model.cost_micros(0, 0, 0) == 0


def test_negative_token_counts_cannot_produce_a_credit():
    model = models.get_model("claude-sonnet-5")
    assert model.cost_micros(-5000, -5000, -5000) == 0


# --- provider message shaping ----------------------------------------------
def _sample_history():
    call = providers.ToolCall("toolu_1", "recommend_titles", {"limit": 3})
    return [
        providers.user_msg("what should I watch"),
        providers.assistant_msg("", [call]),
        providers.tool_msg(call, {"recommendations": []}),
        providers.assistant_msg("Try Overlord."),
    ], call


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


# --- repository hygiene ----------------------------------------------------
def test_chat_engine_sources_contain_no_em_dashes():
    """The no-dash rule applies to what we write, not only to what Lumi writes.

    Cheap to enforce and it keeps the prompt's instruction credible: a codebase
    telling a model to avoid a character it uses everywhere is a smell.
    """
    from pathlib import Path

    engine = Path(__file__).resolve().parents[1] / "chat_engine"
    for path in sorted(engine.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        stripped = text.replace('"' + EM_DASH + '"', "")  # allow a quoted mention
        assert EM_DASH not in stripped, f"em dash in {path.name}"


def test_migration_declares_the_chat_columns():
    """The schema lives only in the migration, so a missing column there is not
    caught by any import or type check."""
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[1] / "migrations" / "002_lumi_chat.sql"
    ).read_text(encoding="utf-8")
    for needle in (
        "chat_enabled",
        "chat_monthly_token_budget",
        "chat_settings",
        "chat_conversations",
        "chat_messages",
        "chat_usage",
    ):
        assert needle in sql
    # Deny-by-default is the whole access model; a DEFAULT TRUE here would hand
    # every account a spending capability.
    assert re.search(r"chat_enabled\s+BOOLEAN\s+NOT NULL\s+DEFAULT FALSE", sql)
