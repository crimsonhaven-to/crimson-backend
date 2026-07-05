"""
Watch-progress dedup keys (_progress_item_key) — the `local:` namespace added for
on-disk media, and proof the existing tv/anime/movie/manga keys are unchanged.

Pure function; importing account_engine.routes needs no live DB (same as the app
import in test_contracts).
"""

from account_engine.routes import _progress_item_key


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
