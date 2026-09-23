


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
