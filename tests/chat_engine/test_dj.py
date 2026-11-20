"""The DJ only ever plays songs from the pool it offered, and keeps playing when
the oracle fails."""

import asyncio

from chat_engine import dj, providers

POOL = [{"id": i, "title": f"Song {i}", "artists": ["Band"], "album": "LP"} for i in range(1, 11)]
SETTINGS = {"provider": "anthropic", "model": None}


def test_a_set_keeps_only_known_songs_in_order():
    text = '{"theme": "Night drive", "intro": "Hello, mortal.", "track_ids": [3, 99, 1, 3, 5]}'
    chosen = dj.parse_set(text, POOL)
    assert chosen["theme"] == "Night drive"
    assert [t["id"] for t in chosen["tracks"]] == [3, 1, 5]


def test_a_set_survives_prose_and_code_fences_around_the_json():
    text = 'Here:\n```json\n{"theme": "x", "intro": "y", "track_ids": [1, 2, 3]}\n```'
    assert len(dj.parse_set(text, POOL)["tracks"]) == 3


def test_an_unusable_answer_is_none():
    assert dj.parse_set("no json at all", POOL) is None
    assert dj.parse_set('{"theme": "x", "track_ids": [1, 99]}', POOL) is None
    assert dj.parse_set('{"theme": "x", "track_ids": "1,2,3"}', POOL) is None


def test_dashes_are_scrubbed_from_what_the_viewer_reads():
    text = '{"theme": "a\u2014b", "intro": "one\u2013two", "track_ids": [1, 2, 3]}'
    chosen = dj.parse_set(text, POOL)
    assert "\u2014" not in chosen["theme"]
    assert "\u2013" not in chosen["intro"]


def test_a_failing_oracle_still_plays_a_set(monkeypatch):
    monkeypatch.setattr(dj.music_store, "random_ready_tracks", lambda user_id, excluded, limit: POOL)

    async def broken(**kwargs):
        raise providers.ProviderError("down")
        yield  # pragma: no cover

    monkeypatch.setattr(dj.providers, "stream_turn", broken)
    monkeypatch.setattr(dj, "provider_key", lambda provider: "key")
    monkeypatch.setattr(dj.voice, "provider_key", lambda provider: None)
    chosen = asyncio.run(dj.next_set({"user_id": 1}, SETTINGS, [], []))
    assert chosen["theme"] == dj.FALLBACK_THEME
    assert len(chosen["tracks"]) == dj.SET_MAX
    assert chosen["voice"] is None


def test_the_intro_she_chose_is_the_one_she_speaks(monkeypatch):
    monkeypatch.setattr(dj.music_store, "random_ready_tracks", lambda user_id, excluded, limit: POOL)

    async def answer(user_id, settings, pool, themes):
        return '{"theme": "x", "intro": "Listen, mortal.", "track_ids": [1, 2, 3]}'

    async def speak(user_id, text):
        return f"wav of {text}"

    monkeypatch.setattr(dj, "_ask", answer)
    monkeypatch.setattr(dj.voice, "speak", speak)
    chosen = asyncio.run(dj.next_set({"user_id": 1}, SETTINGS, [], []))
    assert chosen["voice"] == "wav of Listen, mortal."


def test_a_fully_played_library_starts_over(monkeypatch):
    calls = []

    def pool(user_id, excluded, limit):
        calls.append(list(excluded))
        return [] if excluded else POOL

    monkeypatch.setattr(dj.music_store, "random_ready_tracks", pool)
    assert asyncio.run(dj._pool(1, [1, 2])) == POOL
    assert calls == [[1, 2], []]


def test_an_empty_library_says_so(monkeypatch):
    monkeypatch.setattr(dj.music_store, "random_ready_tracks", lambda user_id, excluded, limit: [])
    try:
        asyncio.run(dj._pool(1, []))
    except dj.EmptyLibrary:
        return
    raise AssertionError("expected EmptyLibrary")
