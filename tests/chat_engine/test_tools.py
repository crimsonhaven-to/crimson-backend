"""Route building is the one piece of frontend knowledge the backend holds, so a
client route rename must fail here rather than in production.
"""

import asyncio





import pytest

from chat_engine import tools
from core.config import get_settings


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
    for schema in tools.TOOL_SCHEMAS + tools.MUSIC_TOOL_SCHEMAS:
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

    result, action = asyncio.run(tools.dispatch("no_such_tool", {}, user_id=1, offered=tools.TOOL_NAMES))
    assert "error" in result
    assert action is None


MUSIC = {t["name"] for t in tools.MUSIC_TOOL_SCHEMAS}


def test_music_tools_are_offered_only_to_a_viewer_with_music(monkeypatch):
    def offered(user):
        return {t["name"] for t in tools.schemas_for(user)}

    assert not offered({"music_enabled": True}) & MUSIC  # no library on this server
    monkeypatch.setenv("MUSIC_ROOT", "/srv/music")
    get_settings.cache_clear()
    assert not offered({"music_enabled": False}) & MUSIC
    assert offered({"music_enabled": True}) >= MUSIC


def test_a_tool_that_was_not_offered_cannot_be_called(monkeypatch):
    calls = []
    monkeypatch.setattr(tools.music_store, "list_playlists", lambda user_id: calls.append(user_id))
    offered = frozenset(t["name"] for t in tools.TOOL_SCHEMAS)
    result, action = asyncio.run(tools.dispatch("find_music", {}, user_id=1, offered=offered))
    assert "error" in result
    assert calls == []


@pytest.fixture
def library(monkeypatch):
    playlists = {
        1: {"id": 1, "name": "Liked Songs", "source": "spotify"},
        2: {"id": 2, "name": "Mine", "source": "local"},
    }
    tracks = [
        {"id": 10, "title": "Ready", "status": "ready"},
        {"id": 11, "title": "Queued", "status": "pending"},
    ]
    monkeypatch.setattr(tools.music_store, "get_playlist",
                        lambda user_id, playlist_id: playlists.get(playlist_id) if user_id == 1 else None)
    monkeypatch.setattr(tools.music_store, "playlist_tracks", lambda playlist_id: tracks)
    monkeypatch.setattr(tools.music_library, "write_playlist_file", lambda playlist: None)
    return playlists


def _run(name, args, user_id=1):
    return asyncio.run(tools.dispatch(name, args, user_id=user_id, offered=tools.TOOL_NAMES))


def test_play_music_hands_the_drawer_a_play_button(library):
    result, action = _run("play_music", {"playlist_id": 1, "track_id": "10", "shuffle": True})
    assert action == {"type": "play_music", "playlist_id": 1, "track_id": 10, "shuffle": True,
                      "label": "Ready · Liked Songs"}
    assert result["ready"] is True


def test_play_music_refuses_what_cannot_play(library):
    assert _run("play_music", {"playlist_id": 1, "track_id": 11})[1] is None
    assert _run("play_music", {"playlist_id": 1}, user_id=2)[1] is None
    assert _run("play_music", {"playlist_id": "nope"})[1] is None


def test_songs_are_only_added_to_an_editable_playlist(library, monkeypatch):
    appended = []
    monkeypatch.setattr(tools.music_store, "append_tracks",
                        lambda user_id, playlist_id, ids: appended.append((playlist_id, ids)) or ids)
    assert "error" in _run("add_to_playlist", {"playlist_id": 1, "track_ids": [10]})[0]
    result, _ = _run("add_to_playlist", {"playlist_id": 2, "track_ids": [10, 11]})
    assert result == {"added": 2, "skipped": 0}
    assert appended == [(2, [10, 11])]


def test_create_playlist_opens_the_new_playlist(library, monkeypatch):
    async def fake_create(user_id, name):
        return {"playlist": {"id": 5, "name": name, "source": "local", "user_id": user_id}}

    monkeypatch.setattr(tools.music_library, "create_local", fake_create)
    monkeypatch.setattr(tools.music_store, "append_tracks", lambda user_id, playlist_id, ids: [])
    result, action = _run("create_playlist", {"name": " Rainy day "})
    assert result == {"playlist_id": 5, "added": 0}
    assert action == {"type": "open", "route": "/music/playlist/5", "label": "Rainy day"}
