



def test_local_anime_fallback_ordering():
    # The AniList-outage fallback (_order_local_anime): non-title sorts surface
    # poster-bearing, newest titles first (a 'trending' stand-in); title sorts stay
    # purely alphabetical regardless of poster/year.
    from metadata_engine.browse import order_local_anime as _order_local_anime

    items = [
        {"title": "z-old-poster", "year": 2000, "poster": "p"},
        {"title": "a-no-poster-new", "year": 2020, "poster": None},
        {"title": "m-poster-new", "year": 2022, "poster": "p"},
    ]

    # trending → poster first, then newest, then title.
    assert [i["title"] for i in _order_local_anime(items, "trending")] == [
        "m-poster-new", "z-old-poster", "a-no-poster-new",
    ]
    # newest → year desc, ignoring poster presence.
    assert [i["title"] for i in _order_local_anime(items, "newest")] == [
        "m-poster-new", "a-no-poster-new", "z-old-poster",
    ]
    # title → pure alphabetical.
    assert [i["title"] for i in _order_local_anime(items, "title")] == [
        "a-no-poster-new", "m-poster-new", "z-old-poster",
    ]
