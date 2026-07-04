"""Manga surface — the public backend's slice (no manga host is contacted here).

The public backend holds NO manga source: discovery is pure AniList and the chapter
list / page images are resolved in the viewer's browser (crimson-sources) or by a
private provider injected only in an operator build. So these tests pin the two
things the public code is actually responsible for:

  * the AniList → card projection the discovery rows depend on (``_manga_item``);
  * the provider seam being genuinely empty in a base build — ``get_provider()`` is
    ``None``, so ``/read`` / ``/manga_proxy`` stay dormant and never reach a host.

The MangaDex client itself (search / chapter feed / @Home / the signed relay + its
SSRF guards) now lives in the private overlay repo; its security tests moved there.
"""

from metadata_engine.anilist import _manga_item


def test_manga_item_projection():
    media = {
        "id": 30002,
        "title": {"romaji": "Berserk", "english": "Berserk", "native": "ベルセルク"},
        "coverImage": {"extraLarge": "big.jpg", "large": "small.jpg"},
        "startDate": {"year": 1989},
        "averageScore": 93,
    }
    item = _manga_item(media)
    assert item["anilist_id"] == 30002
    assert item["title"] == "Berserk"
    assert item["poster"] == "big.jpg"       # prefers extraLarge
    assert item["year"] == 1989
    assert item["vote_average"] == 9.3        # 0-100 score -> 0-10 rating
    assert item["kind"] == "manga"


def test_manga_item_handles_missing_score():
    item = _manga_item({"id": 1, "title": {"romaji": "X"}, "coverImage": {}, "startDate": {}})
    assert item["vote_average"] is None
    assert item["poster"] is None
    assert item["title"] == "X"


def test_no_provider_in_base_build(monkeypatch):
    # A base build ships no manga source: discovery must return None so the routes
    # report "unmapped" and the browser resolves chapters/pages instead. The provider
    # cache is process-wide, so reset it before asserting.
    import manga_engine.provider as provider

    provider._provider_cache.clear()
    assert provider.get_provider() is None
    # Preference config is public (names no host) and has sane defaults.
    assert provider.manga_enabled() is True
    assert provider.default_language() == "en"
    assert "safe" in provider.content_ratings()


def test_candidate_titles_priority_and_dedup():
    from manga_engine.routes import _candidate_titles

    meta = {
        "title_romaji": "Berserk",
        "title_english": "Berserk",      # dup of romaji -> dropped
        "title": "Berserk",              # dup -> dropped
        "title_native": "ベルセルク",
        "synonyms": ["Berserk: The Prototype", None, "ベルセルク"],  # last dup native
    }
    assert _candidate_titles(meta) == ["Berserk", "ベルセルク", "Berserk: The Prototype"]
