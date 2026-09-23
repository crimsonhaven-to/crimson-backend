"""Route building is the one piece of frontend knowledge the backend holds, so a
client route rename must fail here rather than in production.
"""

import asyncio





import pytest

from chat_engine import tools


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


def test_item_key_uses_the_account_engine_scheme():
    assert tools._item_key("anime", 108465, None) == "anilist:108465"
    assert tools._item_key("movie", None, 1014505) == "movie:1014505"
    assert tools._item_key("show", None, 1399) == "tmdb:1399"
    # Only anime key by AniList id, so a stray id on a movie changes nothing.
    assert tools._item_key("movie", 5, 1014505) == "movie:1014505"
    assert tools._item_key("manga", 5, None) is None


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

    result, action = asyncio.run(tools.dispatch("no_such_tool", {}, user_id=1))
    assert "error" in result
    assert action is None
