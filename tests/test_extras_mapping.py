"""Specials/OVAs/films: how an entry finds the show it belongs to.

The mapping used to key on ``themoviedb_id.tv`` alone, which quietly lost most of
a franchise's side content. Overlord is the worked example throughout: the site
sources list three films and nine specials for it, and the mapping produced one
film and three specials, because

  * "The Sacred Kingdom" carries ``{"movie": [1014505]}`` and no ``tv`` key, so it
    had no show to group under and was dropped entirely -- not even an
    anime_entries row;
  * "The Dark Hero" and the Ple Ple Pleiades shorts carry no external id at all,
    so Fribb offers nothing to group them by in the first place.

These pin both recoveries (the tvdb_id bridge and the AniList relations pass)
against fixtures shaped exactly like the real records. No network, no DB.
"""

from metadata_engine.db_handler import MappingDatabaseEngine as Engine

TMDB_SHOW = 64196   # Overlord, on TMDB /tv
TVDB_SHOW = 294002  # ...and the same series on TVDB


def fribb(anilist_id, type_="TV", tmdb=None, tvdb=TVDB_SHOW, season=None):
    """A Fribb `anime-list-full.json` record, in its real shape."""
    return {
        "type": type_,
        "anilist_id": anilist_id,
        "themoviedb_id": tmdb,
        "tvdb_id": tvdb,
        "season": season,
        "mal_id": None,
    }


# Overlord as Fribb actually publishes it: four seasons and two shorts keyed on
# the TMDB show, plus a film keyed on its own TMDB *movie* id.
OVERLORD = [
    fribb(20832, "TV", {"tv": TMDB_SHOW}, season={"tmdb": 1}),
    fribb(98437, "TV", {"tv": TMDB_SHOW}, season={"tmdb": 2}),
    fribb(101474, "TV", {"tv": TMDB_SHOW}, season={"tmdb": 3}),
    fribb(133844, "TV", {"tv": TMDB_SHOW}, season={"tmdb": 4}),
    fribb(87489, "OVA", {"tv": TMDB_SHOW}, season={"tmdb": 0}),
    fribb(98873, "MOVIE", {"tv": TMDB_SHOW}, season={"tmdb": 0}),
    fribb(133845, "MOVIE", {"movie": [1014505]}, season={"tvdb": 0}),
]


# --- the TMDB movie id -----------------------------------------------------
def test_movie_id_read_from_the_list_form():
    """Fribb emits one id per part, so the field is a list even for one film."""
    assert Engine._tmdb_movie_id({"themoviedb_id": {"movie": [1014505]}}) == 1014505


def test_movie_id_is_none_for_a_show():
    assert Engine._tmdb_movie_id({"themoviedb_id": {"tv": TMDB_SHOW}}) is None
    assert Engine._tmdb_movie_id({"themoviedb_id": None}) is None
    assert Engine._tmdb_movie_id({"themoviedb_id": {"movie": []}}) is None


def test_a_film_is_never_mistaken_for_a_show():
    """The two id spaces overlap numerically; reading a movie id as a tv id would
    file the film under whatever unrelated show shares that number."""
    assert Engine._tmdb_tv_id({"themoviedb_id": {"movie": [1014505]}}) is None


# --- grouping --------------------------------------------------------------
def test_film_attaches_to_its_show_through_tvdb_id():
    groups, movie_ids, orphans = Engine._group_by_show(OVERLORD)

    ids = {e["anilist_id"] for e in groups[TMDB_SHOW]}
    assert 133845 in ids, "the film must land on the show, not be dropped"
    assert movie_ids[133845] == 1014505
    assert orphans == set()


def test_a_tvdb_attached_film_never_claims_a_season_slot():
    """It has no `season.tmdb`, and treating it as a season would have it
    overwrite (or be picked over) a real cour of the show."""
    groups, _movie_ids, _orphans = Engine._group_by_show(OVERLORD)

    film = next(e for e in groups[TMDB_SHOW] if e["anilist_id"] == 133845)
    assert film["season_number"] is None

    seasons = {e["season_number"] for e in groups[TMDB_SHOW] if e["season_number"]}
    assert seasons == {1, 2, 3, 4}


def test_seasons_keep_the_season_fribb_gives_them():
    groups, _movie_ids, _orphans = Engine._group_by_show(OVERLORD)
    by_id = {e["anilist_id"]: e for e in groups[TMDB_SHOW]}
    assert by_id[20832]["season_number"] == 1
    assert by_id[133844]["season_number"] == 4


def test_a_film_with_no_parent_becomes_a_standalone_entry():
    """Most anime films are their own franchise. They have no show to sit under,
    but they are still playable off their movie id, so they stay in the dataset
    instead of vanishing from the catalogue."""
    standalone = [fribb(21519, "MOVIE", {"movie": [372058]}, tvdb=None)]

    groups, movie_ids, orphans = Engine._group_by_show(standalone)

    assert groups == {}
    assert orphans == {21519}
    assert movie_ids[21519] == 372058


def test_an_entry_with_no_ids_at_all_is_left_to_the_relations_pass():
    """Fribb gives these nothing to group by; inventing a parent here would be a
    guess. AniList names them instead (see below)."""
    groups, movie_ids, orphans = Engine._group_by_show(
        [fribb(21305, "SPECIAL", tmdb=None, tvdb=None)]
    )
    assert (groups, movie_ids, orphans) == ({}, {}, set())


def test_an_unknown_tvdb_id_does_not_invent_a_show():
    groups, _movie_ids, orphans = Engine._group_by_show(
        [fribb(133845, "MOVIE", {"movie": [1014505]}, tvdb=999999)]
    )
    assert groups == {}
    assert orphans == {133845}


# --- the AniList relations pass --------------------------------------------
def edge(relation, node_id, fmt):
    return {"relationType": relation, "node": {"id": node_id, "format": fmt}}


def test_relations_recover_side_content_fribb_cannot_key():
    """The Dark Hero and Ple Ple Pleiades hang off Overlord season 1 as SUMMARY /
    SIDE_STORY edges. Nothing else in the dataset connects them to the show."""
    season_rows = [(TMDB_SHOW, 1, 20832)]
    metadata = {
        20832: {"relations": {"edges": [
            edge("SUMMARY", 98874, "MOVIE"),
            edge("SIDE_STORY", 21305, "SPECIAL"),
        ]}}
    }

    found = Engine._relation_extras(season_rows, [], metadata)

    assert {(t, a) for t, a, _f, _n in found} == {(TMDB_SHOW, 98874), (TMDB_SHOW, 21305)}


def test_relations_are_walked_from_every_season_not_just_the_first():
    """The Sacred Kingdom hangs off Overlord IV, so anchoring on season 1 alone
    would still miss it."""
    season_rows = [(TMDB_SHOW, 1, 20832), (TMDB_SHOW, 4, 133844)]
    metadata = {
        20832: {"relations": {"edges": []}},
        133844: {"relations": {"edges": [edge("SIDE_STORY", 133845, "MOVIE")]}},
    }

    found = Engine._relation_extras(season_rows, [], metadata)

    assert [a for _t, a, _f, _n in found] == [133845]


def test_crossovers_and_sequels_are_not_side_content():
    """Isekai Quartet is a CHARACTER edge -- a crossover, not an Overlord special
    -- and a SEQUEL is a season or a show of its own."""
    season_rows = [(TMDB_SHOW, 1, 20832)]
    metadata = {
        20832: {"relations": {"edges": [
            edge("CHARACTER", 104454, "TV_SHORT"),
            edge("CHARACTER", 117074, "MOVIE"),
            edge("SEQUEL", 98437, "TV"),
            edge("ADAPTATION", 85976, "NOVEL"),
            edge("SOURCE", 85934, "MANGA"),
        ]}}
    }

    assert Engine._relation_extras(season_rows, [], metadata) == []


def test_a_side_story_pointing_at_a_full_series_is_rejected():
    """The relation type alone is not enough: a SIDE_STORY can point at a spin-off
    TV series, which is its own show and not this one's special."""
    season_rows = [(TMDB_SHOW, 1, 20832)]
    metadata = {20832: {"relations": {"edges": [edge("SIDE_STORY", 999, "TV")]}}}

    assert Engine._relation_extras(season_rows, [], metadata) == []


def test_an_entry_that_owns_a_season_elsewhere_is_never_refiled_as_an_extra():
    """It is a series in its own right; listing it as somebody's special would
    duplicate it and give it two homes."""
    season_rows = [(TMDB_SHOW, 1, 20832), (555, 1, 777)]
    metadata = {20832: {"relations": {"edges": [edge("SIDE_STORY", 777, "ONA")]}}}

    assert Engine._relation_extras(season_rows, [], metadata) == []


def test_an_extra_already_known_from_fribb_is_not_added_twice():
    season_rows = [(TMDB_SHOW, 1, 20832)]
    extra_rows = [(TMDB_SHOW, 87489, "OVA", None)]
    metadata = {20832: {"relations": {"edges": [edge("SIDE_STORY", 87489, "OVA")]}}}

    assert Engine._relation_extras(season_rows, extra_rows, metadata) == []


def test_the_same_extra_reached_from_two_seasons_is_added_once():
    season_rows = [(TMDB_SHOW, 1, 20832), (TMDB_SHOW, 2, 98437)]
    metadata = {
        20832: {"relations": {"edges": [edge("SIDE_STORY", 21305, "SPECIAL")]}},
        98437: {"relations": {"edges": [edge("SIDE_STORY", 21305, "SPECIAL")]}},
    }

    assert [a for _t, a, _f, _n in Engine._relation_extras(season_rows, [], metadata)] == [21305]


def test_the_edge_node_is_returned_so_no_second_anilist_fetch_is_needed():
    season_rows = [(TMDB_SHOW, 1, 20832)]
    node = {"id": 21305, "format": "SPECIAL", "title": {"romaji": "Overlord: Ple Ple Pleiades"}}
    metadata = {20832: {"relations": {"edges": [{"relationType": "SIDE_STORY", "node": node}]}}}

    found = Engine._relation_extras(season_rows, [], metadata)

    assert found[0][2] == "SPECIAL"
    assert found[0][3] is node


def test_missing_or_malformed_relations_are_survivable():
    """Metadata is best-effort: a chunk that failed leaves ids with no relations
    at all, and that must not take the whole resync down."""
    season_rows = [(TMDB_SHOW, 1, 20832), (TMDB_SHOW, 2, 98437)]
    metadata = {
        20832: {},                                  # fetched, no relations key
        98437: {"relations": {"edges": [None, {}]}},  # edges present but junk
    }

    assert Engine._relation_extras(season_rows, extra_rows=[], al_metadata=metadata) == []
