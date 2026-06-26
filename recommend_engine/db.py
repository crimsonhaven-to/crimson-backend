"""
Recommendation data layer.

Builds an in-process "catalogue index" from the metadata tables the rest of the
backend already maintains:

  * anime    — genres on ``anime_entries`` mapped to shows via ``tmdb_seasons`` /
               ``tmdb_extras`` (see metadata_engine.db_handler).
  * shows    — genres on ``tmdb_shows`` (lazily populated by fetch_tmdb_show and
               the trending/search discovery, see api.py).
  * movies   — genres on ``tmdb_movies`` (same lazy population).

Nothing here writes, no schema is added, and no external API is called:
recommendations are derived purely from genres already in the database. Each of
the three surfaces is scored within its own genre vocabulary (AniList genres and
TMDB tv/movie genres differ), and the routes layer merges the three by score.

The index only changes as titles get opened / resynced, so it's cached
in-process for ``CACHE_TTL`` seconds — turning each request into a CPU pass over
in-memory dicts instead of a multi-thousand-row scan + JSON parse.
"""

import json
import threading
import time
from typing import Dict, FrozenSet, List, Optional

from db_pool import get_connection

CACHE_TTL = 1800  # seconds (30 min)

_lock = threading.Lock()
_cache: Optional["CatalogueIndex"] = None
_cache_at: float = 0.0


class CatalogueIndex:
    """Immutable snapshot of the local catalogue used for scoring.

    Genre maps (for resolving a *seed* to its genres):
      * ``genres_by_anilist`` — anilist_id -> genres (every anime entry).
      * ``genres_by_show``    — non-anime show tmdb_id -> genres.
      * ``genres_by_movie``   — movie tmdb_id -> genres.
    ``tmdb_by_anilist`` collapses an anime seed (any season) to its show tmdb_id.

    Candidate lists (the things we actually recommend), each one row per title
    with a genre set, display title, year and poster path:
      * ``anime_candidates`` / ``show_candidates`` / ``movie_candidates``.
    """

    def __init__(self, *, genres_by_anilist, genres_by_show, genres_by_movie,
                 tmdb_by_anilist, anime_candidates, show_candidates, movie_candidates):
        self.genres_by_anilist: Dict[int, FrozenSet[str]] = genres_by_anilist
        self.genres_by_show: Dict[int, FrozenSet[str]] = genres_by_show
        self.genres_by_movie: Dict[int, FrozenSet[str]] = genres_by_movie
        self.tmdb_by_anilist: Dict[int, int] = tmdb_by_anilist
        self.anime_candidates: List[Dict] = anime_candidates
        self.show_candidates: List[Dict] = show_candidates
        self.movie_candidates: List[Dict] = movie_candidates


def _parse_genres(raw) -> FrozenSet[str]:
    """Parse a JSON-encoded genres column into a set (``frozenset()`` on junk)."""
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
    """Best-effort 4-digit year from a 'YYYY-MM-DD' TMDB date string."""
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

        # --- anime -----------------------------------------------------
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
        lowest_season: Dict[int, Dict] = {}  # tmdb_id -> {anilist_id, season_number}
        for r in cur.fetchall():
            tmdb_by_anilist.setdefault(r["anilist_id"], r["tmdb_id"])
            lowest_season.setdefault(
                r["tmdb_id"],
                {"anilist_id": r["anilist_id"], "season_number": r["season_number"]},
            )

        cur.execute("SELECT anilist_id, tmdb_id FROM tmdb_extras")
        for r in cur.fetchall():
            tmdb_by_anilist.setdefault(r["anilist_id"], r["tmdb_id"])

        # --- non-anime shows + movies ----------------------------------
        cur.execute(
            "SELECT tmdb_id, title, poster_path, first_air_date, genres FROM tmdb_shows"
        )
        show_rows = cur.fetchall()
        cur.execute(
            "SELECT tmdb_id, title, poster_path, release_date, genres FROM tmdb_movies"
        )
        movie_rows = cur.fetchall()

        # Posters for anime candidates come from tmdb_shows (sparse).
        posters = {r["tmdb_id"]: r["poster_path"] for r in show_rows}

    # anime candidates: one per show (lowest season) that has genres AND a poster.
    # The poster comes from tmdb_shows, which is sparse (only titles opened at least
    # once), so we skip posterless candidates — exactly like /trending and /search do
    # (a posterless tile just renders a "No Sigil" placeholder on the home rows).
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
        # A seed still needs its genres even if it has no poster, so register the
        # genre lookup unconditionally; only the *candidate* (a tile we'd render)
        # requires a poster.
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


def get_catalogue_index(force: bool = False) -> CatalogueIndex:
    """Return the cached catalogue index, rebuilding it if stale (or forced).

    Synchronous (borrows a pooled connection); call from a threadpool in async
    handlers like the rest of this backend's DB access.
    """
    global _cache, _cache_at
    now = time.time()
    if not force and _cache is not None and (now - _cache_at) < CACHE_TTL:
        return _cache
    with _lock:
        if force or _cache is None or (time.time() - _cache_at) >= CACHE_TTL:
            _cache = _build_index()
            _cache_at = time.time()
    return _cache
