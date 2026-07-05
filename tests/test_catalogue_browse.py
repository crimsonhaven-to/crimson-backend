"""Pure-logic tests for the shows/movies/manga browse catalogues.

These cover the parts of the new /catalogue/shows|movies|manga endpoints that
don't need a DB or the network: the defensive genre decode, the popular-first
sort key, the manga sort-token map, and the AniList manga projection. The DB
readers themselves (get_shows_catalogue_items / get_movies_catalogue_items) are
thin SELECT + project loops exercised end-to-end by the running service.
"""

from web.queries import _decode_genres
from metadata_engine.anilist import _MEDIA_SORTS, CATALOGUE_DEFAULT_SORT, MANGA_DEFAULT_SORT, _manga_item


def test_decode_genres_is_defensive():
    assert _decode_genres('["Drama", "Crime"]') == ["Drama", "Crime"]
    assert _decode_genres(None) == []          # rows synced before genres existed
    assert _decode_genres("") == []
    assert _decode_genres("not json") == []    # malformed → empty, never raises


def _sort_key(x):
    # Mirror of the ORDER used by get_shows/movies_catalogue_items: popularity
    # desc (NULLS LAST via the -inf sentinel), then newest year, then title.
    return (
        -(x["popularity"] if isinstance(x["popularity"], (int, float)) else float("-inf")),
        -(int(x["year"]) if (x["year"] or "").isdigit() else 0),
        (x["title"] or "").lower(),
    )


def test_popularity_sort_puts_popular_first_and_nulls_last():
    rows = [
        {"popularity": None, "year": "2001", "title": "b"},
        {"popularity": 5.0, "year": "1999", "title": "a"},
        {"popularity": 50.0, "year": "2000", "title": "c"},
    ]
    assert [r["title"] for r in sorted(rows, key=_sort_key)] == ["c", "a", "b"]


def test_media_sort_tokens_map_to_anilist_enums():
    # Shared by the anime + manga AniList browses.
    assert set(_MEDIA_SORTS) == {"trending", "popular", "score", "newest", "title"}
    assert _MEDIA_SORTS[CATALOGUE_DEFAULT_SORT] == "TRENDING_DESC"
    assert _MEDIA_SORTS["score"] == "SCORE_DESC"
    # Back-compat alias still points at the default.
    assert MANGA_DEFAULT_SORT == CATALOGUE_DEFAULT_SORT


def test_manga_item_projection():
    node = {
        "id": 30013,
        "title": {"english": "One Piece", "romaji": "One Piece"},
        "coverImage": {"extraLarge": "u", "large": "l"},
        "startDate": {"year": 1997},
        "averageScore": 90,
    }
    assert _manga_item(node) == {
        "anilist_id": 30013,
        "title": "One Piece",
        "poster": "u",
        "year": 1997,
        "vote_average": 9.0,  # AniList 0-100 → card 0-10
        "kind": "manga",
    }
