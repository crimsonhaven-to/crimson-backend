"""The in-process index the recommender scores against, built from tables the
metadata engine already maintains: genres on ``anime_entries`` (tied to shows by
``tmdb_seasons`` and ``tmdb_extras``), ``tmdb_shows`` and ``tmdb_movies``.

Read-only, no external calls. AniList and TMDB tv/movie genres are different
vocabularies, so each surface is scored separately and the service merges them.
The index only changes as titles are opened or resynced, so it is cached for
``CACHE_TTL`` rather than rescanning and re-parsing thousands of rows per request.
"""

import json
import threading
import time
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional

from core.db_pool import get_connection

CACHE_TTL = 1800

_lock = threading.Lock()
_cache: Optional["CatalogueIndex"] = None
_cache_at: float = 0.0


@dataclass(frozen=True)
class CatalogueIndex:
    # Seed lookups: a seed resolves to its genres through these.
    genres_by_anilist: Dict[int, FrozenSet[str]]
    genres_by_show: Dict[int, FrozenSet[str]]
    genres_by_movie: Dict[int, FrozenSet[str]]
    # An anime seed from any season collapses to its show.
    tmdb_by_anilist: Dict[int, int]
    # What can be recommended: one postered row per title, with its genre set.
    anime_candidates: List[Dict]
    show_candidates: List[Dict]
    movie_candidates: List[Dict]


def _parse_genres(raw) -> FrozenSet[str]:
    if not raw:
        return frozenset()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return frozenset()
    if not isinstance(data, list):
        return frozenset()
    return frozenset(g for g in data if isinstance(g, str) and g)


def _year(date_str) -> Optional[int]:
    if not date_str or len(str(date_str)) < 4:
        return None
    try:
        return int(str(date_str)[:4])
    except (TypeError, ValueError):
        return None


def _build_index() -> CatalogueIndex:
    genres_by_anilist: Dict[int, FrozenSet[str]] = {}
    entries: Dict[int, Dict] = {}

    with get_connection() as conn:
        cur = conn.cursor()

        cur.execute(
            "SELECT anilist_id, title_romaji, title_english, title_native, "
            "anime_type, start_year, genres FROM anime_entries"
        )
        for r in cur.fetchall():
            g = _parse_genres(r["genres"])
            if g:
                genres_by_anilist[r["anilist_id"]] = g
            entries[r["anilist_id"]] = r

        cur.execute(
            "SELECT tmdb_id, season_number, anilist_id FROM tmdb_seasons "
            "ORDER BY tmdb_id, season_number"
        )
        tmdb_by_anilist: Dict[int, int] = {}
        lowest_season: Dict[int, Dict] = {}
        for r in cur.fetchall():
            tmdb_by_anilist.setdefault(r["anilist_id"], r["tmdb_id"])
            lowest_season.setdefault(
                r["tmdb_id"],
                {"anilist_id": r["anilist_id"], "season_number": r["season_number"]},
            )

        cur.execute("SELECT anilist_id, tmdb_id FROM tmdb_extras")
        for r in cur.fetchall():
            tmdb_by_anilist.setdefault(r["anilist_id"], r["tmdb_id"])

        cur.execute(
            "SELECT tmdb_id, title, poster_path, first_air_date, genres FROM tmdb_shows"
        )
        show_rows = cur.fetchall()
        cur.execute(
            "SELECT tmdb_id, title, poster_path, release_date, genres FROM tmdb_movies"
        )
        movie_rows = cur.fetchall()

        posters = {r["tmdb_id"]: r["poster_path"] for r in show_rows}

    # One anime candidate per show, its lowest season. The poster comes from
    # tmdb_shows, which only holds titles seen at least once, and a posterless
    # candidate is skipped as /trending and /search skip them: the tile would
    # render as a placeholder.
    anime_candidates: List[Dict] = []
    for tmdb_id, sel in lowest_season.items():
        anilist_id = sel["anilist_id"]
        genres = genres_by_anilist.get(anilist_id)
        if not genres:
            continue
        poster_path = posters.get(tmdb_id)
        if not poster_path:
            continue
        e = entries.get(anilist_id, {})
        anime_candidates.append({
            "kind": "anime",
            "tmdb_id": tmdb_id,
            "anilist_id": anilist_id,
            "season_number": sel["season_number"],
            "title": e.get("title_english") or e.get("title_romaji") or e.get("title_native"),
            "year": e.get("start_year"),
            "poster_path": poster_path,
            "genres": genres,
        })

    genres_by_show: Dict[int, FrozenSet[str]] = {}
    show_candidates: List[Dict] = []
    for r in show_rows:
        g = _parse_genres(r["genres"])
        if not g:
            continue
        # A posterless title can still be a seed; only a candidate needs a poster.
        genres_by_show[r["tmdb_id"]] = g
        if not r["poster_path"]:
            continue
        show_candidates.append({
            "kind": "show",
            "tmdb_id": r["tmdb_id"],
            "anilist_id": None,
            "title": r["title"],
            "year": _year(r["first_air_date"]),
            "poster_path": r["poster_path"],
            "genres": g,
        })

    genres_by_movie: Dict[int, FrozenSet[str]] = {}
    movie_candidates: List[Dict] = []
    for r in movie_rows:
        g = _parse_genres(r["genres"])
        if not g:
            continue
        genres_by_movie[r["tmdb_id"]] = g
        if not r["poster_path"]:
            continue
        movie_candidates.append({
            "kind": "movie",
            "tmdb_id": r["tmdb_id"],
            "anilist_id": None,
            "title": r["title"],
            "year": _year(r["release_date"]),
            "poster_path": r["poster_path"],
            "genres": g,
        })

    return CatalogueIndex(
        genres_by_anilist=genres_by_anilist,
        genres_by_show=genres_by_show,
        genres_by_movie=genres_by_movie,
        tmdb_by_anilist=tmdb_by_anilist,
        anime_candidates=anime_candidates,
        show_candidates=show_candidates,
        movie_candidates=movie_candidates,
    )


def get_catalogue_index() -> CatalogueIndex:
    """The cached index, rebuilt once stale. Synchronous."""
    global _cache, _cache_at
    if _cache is not None and time.monotonic() - _cache_at < CACHE_TTL:
        return _cache
    with _lock:
        # Another thread may have rebuilt it while this one waited for the lock.
        if _cache is None or time.monotonic() - _cache_at >= CACHE_TTL:
            _cache = _build_index()
            _cache_at = time.monotonic()
    return _cache
