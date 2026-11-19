"""Watch-progress dedup keys: the `local:` namespace for on-disk media, and the
tv/anime/movie/manga keys it must not change."""


import account_engine.library as library
from account_engine.library import progress_item_key as _progress_item_key


def test_local_movie_key_is_one_row_per_title():
    # No season/episode -> a single row per local title.
    assert _progress_item_key(None, None, None, None, "local", "TOKENabc") == "local:TOKENabc"


def test_local_show_key_is_per_episode():
    key = _progress_item_key(None, None, 2, 5, "local", "TOKENabc")
    assert key == "local:TOKENabc:s2:e5"
    # A different episode of the same title is a distinct row...
    assert _progress_item_key(None, None, 2, 6, "local", "TOKENabc") != key
    # ...but they share the title prefix _dedup_by_show collapses on.
    assert key.startswith("local:TOKENabc")


def test_local_requires_a_token():
    # Without a token the local branch is skipped (no id at all -> tmdb fallback).
    assert _progress_item_key(None, None, None, None, "local", None) == "tmdb:None"


def test_non_local_keys_are_unchanged():
    assert _progress_item_key(100, 200, 1, 3, None) == "anilist:200:s1:e3"
    assert _progress_item_key(100, None, 1, 3, None) == "tmdb:100:s1:e3"
    assert _progress_item_key(555, None, None, None, "movie") == "movie:555"
    assert _progress_item_key(None, 900, None, 4, "manga") == "manga:900"


# --- a favorite is the show, not one of its seasons ------------------------------------

_OVERLORD = {146984: 20832, 101474: 20832, 20832: 20832}


def _seasons(monkeypatch):
    def first(anilist_id):
        to = _OVERLORD.get(anilist_id)
        return {"anilist_id": to, "title": "Overlord"} if to else None
    monkeypatch.setattr(library.catalogue, "first_season_entry", first)


def _fav(**overrides):
    fav = {"item_key": "anilist:146984", "tmdb_id": None, "anilist_id": 146984,
           "media_type": None, "title": "Overlord IV", "poster": "p4"}
    fav.update(overrides)
    return fav


def test_a_later_season_is_stored_as_the_first_with_its_title(monkeypatch):
    _seasons(monkeypatch)
    assert library.as_show_favorite(_fav()) == _fav(
        item_key="anilist:20832", anilist_id=20832, title="Overlord",
    )


def test_the_first_season_and_unmapped_ids_are_left_alone(monkeypatch):
    _seasons(monkeypatch)
    first = _fav(item_key="anilist:20832", anilist_id=20832, title="Mine")
    assert library.as_show_favorite(first) == first
    film = _fav(item_key="anilist:7", anilist_id=7)
    assert library.as_show_favorite(film) == film


def test_manga_and_tmdb_rows_never_look_up_a_season(monkeypatch):
    monkeypatch.setattr(library.catalogue, "first_season_entry", lambda _: 1 / 0)
    manga = _fav(item_key="manga:146984", media_type="manga")
    show = _fav(item_key="tmdb:5", anilist_id=None, tmdb_id=5)
    assert library.as_show_favorite(manga) == manga
    assert library.as_show_favorite(show) == show


def test_any_season_removes_the_show(monkeypatch):
    _seasons(monkeypatch)
    assert library.show_favorite_key(None, 101474) == "anilist:20832"
    assert library.show_favorite_key(None, 146984, "manga") == "manga:146984"
    assert library.show_favorite_key(5, None, "movie") == "movie:5"
