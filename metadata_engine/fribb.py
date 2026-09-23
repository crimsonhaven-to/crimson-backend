"""Turning the Fribb anime-lists dataset into season and extra rows. Pure: no
network, no database.

TMDB groups an anime as one show with numbered seasons, while AniList gives every
cour, OVA and movie its own id. Fribb gives each AniList entry its parent
``themoviedb_id.tv`` and the TMDB season it maps to. That is trusted for real TV
seasons; everything else tied to the show becomes an extra, as do the losers of a
season collision, so nothing is lost.

``themoviedb_id.tv`` alone loses most of a franchise's side content, two ways:

1. A film TMDB tracks as a standalone movie carries a movie id and no ``tv`` key.
   When it repeats its parent's ``tvdb_id`` it is attached through a
   tvdb_id -> tmdb_id map, keeping its movie id so it plays through the movie
   route. A film with no parent still gets an ``anime_entries`` row.
2. Roughly a third of the dataset carries no external id at all. AniList names
   those through the ``relations`` of the ids already mapped, which
   ``relation_extras`` turns into extras.
"""

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("crimson.mapping")

# Side content only. Excluded on purpose: SEQUEL and PREQUEL (a season or a show
# in its own right), CHARACTER (crossovers), ADAPTATION and SOURCE (the novel),
# and OTHER (too noisy). PARENT catches an entry pointing up at the main series.
EXTRA_RELATIONS = {"SIDE_STORY", "SUMMARY", "SPECIAL", "PARENT", "ALTERNATIVE"}

# The related entry must itself be side content, which keeps a SIDE_STORY edge
# pointing at a full spin-off TV series out of the extras.
EXTRA_FORMATS = {"SPECIAL", "OVA", "ONA", "MOVIE"}

SeasonRow = Tuple[int, int, int]  # (tmdb_id, season_number, anilist_id)
ExtraRow = Tuple[int, int, str, Optional[int]]  # (tmdb_id, anilist_id, anime_type, tmdb_movie_id)


def safe_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        if isinstance(value, str) and "." in value:
            value = value.split(".")[0]
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def tmdb_tv_id(item: Dict[str, Any]) -> Optional[int]:
    raw = item.get("themoviedb_id")
    if isinstance(raw, dict):
        return safe_int(raw.get("tv"))
    return safe_int(raw)


def tmdb_movie_id(item: Dict[str, Any]) -> Optional[int]:
    """Fribb lists one movie id per part of a split release; the first one plays."""
    raw = item.get("themoviedb_id")
    if not isinstance(raw, dict):
        return None
    movie = raw.get("movie")
    if isinstance(movie, list):
        movie = movie[0] if movie else None
    return safe_int(movie)


def tmdb_season(item: Dict[str, Any]) -> Optional[int]:
    season = item.get("season")
    if isinstance(season, dict):
        return safe_int(season.get("tmdb"))
    return safe_int(season)


def group_by_show(anime_data: List[Dict[str, Any]]):
    """Bucket the dataset by TMDB show.

    Returns ``(groups, movie_id_by_anilist, orphan_movies)``: every film with a
    movie id, parent or not, is in the second; films with no parent, which still
    deserve a catalogue row, are in the third.

    An entry attached through its ``tvdb_id`` is side content by definition (a
    real season always carries its own ``themoviedb_id.tv``), so its season is
    forced to None and it lands in the extras.
    """
    # First writer wins, since a tvdb series maps to exactly one TMDB show.
    tvdb_to_tmdb: Dict[int, int] = {}
    for item in anime_data:
        tvdb_id = safe_int(item.get("tvdb_id"))
        tmdb_id = tmdb_tv_id(item)
        if tvdb_id and tmdb_id:
            tvdb_to_tmdb.setdefault(tvdb_id, tmdb_id)

    groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    movie_id_by_anilist: Dict[int, int] = {}
    orphan_movies: Set[int] = set()

    for item in anime_data:
        anilist_id = safe_int(item.get("anilist_id"))
        if not anilist_id:
            continue
        movie_id = tmdb_movie_id(item)
        if movie_id:
            movie_id_by_anilist.setdefault(anilist_id, movie_id)

        tmdb_id = tmdb_tv_id(item)
        attached_by_tvdb = False
        if not tmdb_id:
            tvdb_id = safe_int(item.get("tvdb_id"))
            tmdb_id = tvdb_to_tmdb.get(tvdb_id) if tvdb_id else None
            attached_by_tvdb = tmdb_id is not None

        if not tmdb_id:
            # Anything that is not a film waits for the relations pass.
            if movie_id:
                orphan_movies.add(anilist_id)
            continue

        groups[tmdb_id].append(
            {
                "anilist_id": anilist_id,
                "season_number": None if attached_by_tvdb else tmdb_season(item),
                "mal_id": safe_int(item.get("mal_id")),
                "type": (item.get("type") or "TV").upper(),
            }
        )

    return groups, movie_id_by_anilist, orphan_movies


def _wins_slot(candidate: Dict, current: Optional[Dict]) -> bool:
    """A real TV entry first, then the lowest AniList id, so collisions resolve
    the same way on every rebuild."""
    if current is None:
        return True
    candidate_tv = candidate["type"] == "TV"
    current_tv = current["type"] == "TV"
    if candidate_tv != current_tv:
        return candidate_tv
    return candidate["anilist_id"] < current["anilist_id"]


def assign_seasons(
    groups: Dict[int, List[Dict[str, Any]]], movie_id_by_anilist: Dict[int, int]
) -> Tuple[List[SeasonRow], List[ExtraRow]]:
    """Give each show one AniList id per season slot and file the rest as extras."""
    season_rows: List[SeasonRow] = []
    extra_rows: List[ExtraRow] = []

    for tmdb_id, items in groups.items():
        chosen: Dict[int, Dict] = {}
        leftovers: List[Dict] = []

        for entry in items:
            season = entry["season_number"]
            if season is None or season < 1:
                leftovers.append(entry)
            elif _wins_slot(entry, chosen.get(season)):
                if season in chosen:
                    leftovers.append(chosen[season])
                chosen[season] = entry
            else:
                leftovers.append(entry)

        # A show Fribb gave no season slot still has a first season if it has a TV entry.
        if not chosen:
            tv_entries = [e for e in leftovers if e["type"] == "TV"]
            if tv_entries:
                first = min(tv_entries, key=lambda e: e["anilist_id"])
                chosen[1] = first
                leftovers.remove(first)

        for season, entry in chosen.items():
            season_rows.append((tmdb_id, season, entry["anilist_id"]))
        for entry in leftovers:
            extra_rows.append((tmdb_id, entry["anilist_id"], entry["type"],
                               movie_id_by_anilist.get(entry["anilist_id"])))

    return season_rows, extra_rows


def relation_extras(season_rows: List[SeasonRow], extra_rows: List[ExtraRow],
                    al_metadata: Dict[int, Dict]) -> List[tuple]:
    """Each show's side content found among the AniList relations of the entries
    already mapped to it, as ``(tmdb_id, anilist_id, format, node)`` for the new
    extras only. The edge node doubles as that entry's metadata, so it needs no
    second fetch.

    Walks one hop out from every mapped member, which is what picks up a film
    hanging off a later season. It deliberately does not walk the discovered
    entries in turn: their relations were never fetched, and following them would
    drift into neighbouring franchises.
    """
    members: Dict[int, List[int]] = defaultdict(list)
    for tmdb_id, _season, anilist_id in season_rows:
        members[tmdb_id].append(anilist_id)
    for tmdb_id, anilist_id, *_rest in extra_rows:
        members[tmdb_id].append(anilist_id)

    # An id that owns a season slot is a series in its own right, never somebody's special.
    season_ids = {anilist_id for _t, _s, anilist_id in season_rows}
    claimed = {(tmdb_id, anilist_id) for tmdb_id, anilist_id, *_r in extra_rows}

    found: List[tuple] = []
    for tmdb_id, member_ids in members.items():
        for member_id in member_ids:
            relations = (al_metadata.get(member_id) or {}).get("relations") or {}
            for edge in relations.get("edges") or []:
                if (edge or {}).get("relationType") not in EXTRA_RELATIONS:
                    continue
                node = edge.get("node") or {}
                node_id = safe_int(node.get("id"))
                node_format = node.get("format")
                if not node_id or node_format not in EXTRA_FORMATS:
                    continue
                if node_id in season_ids or (tmdb_id, node_id) in claimed:
                    continue
                claimed.add((tmdb_id, node_id))
                found.append((tmdb_id, node_id, node_format, node))
    return found


def load_overrides(path: Path) -> Dict[int, Dict[int, int]]:
    """overrides.json as ``{tmdb_id: {season_number: anilist_id}}``."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Could not read {path.name}: {e}")
        return {}

    parsed: Dict[int, Dict[int, int]] = {}
    for tmdb_key, seasons in (raw.get("seasons") or {}).items():
        tmdb_id = safe_int(tmdb_key)
        if tmdb_id is None or not isinstance(seasons, dict):
            continue
        season_map: Dict[int, int] = {}
        for season_key, anilist_value in seasons.items():
            season = safe_int(season_key)
            anilist_id = safe_int(anilist_value)
            if season is not None and anilist_id is not None:
                season_map[season] = anilist_id
        if season_map:
            parsed[tmdb_id] = season_map
    return parsed
