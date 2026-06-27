"""
Local store for discovered TMDB metadata (the non-anime shows/movies surface),
lifted out of api.py.

Reads/writes the ``tmdb_shows`` / ``tmdb_movies`` tables and the ``tmdb_seasons``
mapping (``get_first_anilist_ids``), and lazily persists discover/search results
so they can later become recommendation candidates. The TMDB fetchers
(metadata_engine.tmdb) are the callers.
"""

import json
import logging
from typing import Dict, List

from core.db_pool import get_connection

logger = logging.getLogger("crimson.store")


def get_first_anilist_ids(tmdb_ids: List[int]) -> Dict[int, int]:
    """Map each tmdb_id -> its lowest-numbered season's anilist_id, in ONE query.

    Replaces calling ``get_show_seasons`` once per search/trending result (an N+1
    that borrowed a pooled connection per item). A tmdb_id with no mapped season
    is simply absent from the result.
    """
    if not tmdb_ids:
        return {}
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT tmdb_id, anilist_id, season_number
                   FROM tmdb_seasons
                   WHERE tmdb_id = ANY(%s)
                   ORDER BY tmdb_id, season_number""",
                (list(tmdb_ids),),
            )
            out: Dict[int, int] = {}
            for r in cursor.fetchall():
                out.setdefault(r["tmdb_id"], r["anilist_id"])  # first = lowest season
            return out
    except Exception as e:
        logger.error(f"Database error in get_first_anilist_ids: {e}")
        return {}


def upsert_show_info(show: Dict) -> None:
    """Persist TMDB show details fetched on demand (lazy population of tmdb_shows)."""
    if not show.get("tmdb_id"):
        return
    try:
        # genres is a JSON list of names; only overwrite the stored value when the
        # caller actually supplies one, so a later metadata refresh that omits it
        # (e.g. the degraded path) doesn't blank out genres we already have.
        genres = show.get("genres")
        genres_json = json.dumps(genres) if genres else None
        def _write():
            with get_connection() as conn:
                conn.execute("""
                    INSERT INTO tmdb_shows
                        (tmdb_id, title, overview, poster_path, backdrop_path, first_air_date, genres)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tmdb_id) DO UPDATE SET
                        title=EXCLUDED.title, overview=EXCLUDED.overview,
                        poster_path=EXCLUDED.poster_path, backdrop_path=EXCLUDED.backdrop_path,
                        first_air_date=EXCLUDED.first_air_date,
                        genres=COALESCE(EXCLUDED.genres, tmdb_shows.genres)
                """, (
                    show.get("tmdb_id"),
                    show.get("title"),
                    show.get("overview"),
                    show.get("poster_path"),
                    show.get("backdrop_path"),
                    show.get("first_air_date"),
                    genres_json,
                ))
        _write()
    except Exception as e:
        logger.error(f"Database error in upsert_show_info: {e}")


def upsert_movie_info(movie: Dict) -> None:
    """Persist TMDB movie details fetched on demand (lazy population of
    tmdb_movies), mirroring upsert_show_info. Used as the TMDB-down fallback for
    /movie-overview + /watch/movie (a movie has no AniList entry to fall back on)."""
    if not movie.get("tmdb_id"):
        return
    try:
        genres = movie.get("genres")
        genres_json = json.dumps(genres) if genres else None
        def _write():
            with get_connection() as conn:
                conn.execute("""
                    INSERT INTO tmdb_movies
                        (tmdb_id, title, overview, poster_path, backdrop_path, release_date, genres)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tmdb_id) DO UPDATE SET
                        title=EXCLUDED.title, overview=EXCLUDED.overview,
                        poster_path=EXCLUDED.poster_path, backdrop_path=EXCLUDED.backdrop_path,
                        release_date=EXCLUDED.release_date,
                        genres=COALESCE(EXCLUDED.genres, tmdb_movies.genres)
                """, (
                    movie.get("tmdb_id"),
                    movie.get("title"),
                    movie.get("overview"),
                    movie.get("poster_path"),
                    movie.get("backdrop_path"),
                    movie.get("release_date"),
                    genres_json,
                ))
        _write()
    except Exception as e:
        logger.error(f"Database error in upsert_movie_info: {e}")


def _genre_names(item: Dict, genre_map: Dict[int, str]) -> List[str]:
    """Resolve a discover/search item's genre_ids to names via ``genre_map``."""
    return [genre_map[g] for g in (item.get("genre_ids") or []) if g in genre_map]


def _persist_discovered_show(item: Dict, genre_map: Dict[int, str]) -> None:
    """Lazily cache a discovered/searched non-anime show into tmdb_shows (title,
    overview, art, genres) so it can later be a recommendation candidate without a
    full overview open. Best-effort; mirrors fetch_tmdb_show's upsert."""
    if not item.get("id"):
        return
    upsert_show_info({
        "tmdb_id": item.get("id"),
        "title": item.get("name") or item.get("original_name"),
        "overview": item.get("overview"),
        "poster_path": item.get("poster_path"),
        "backdrop_path": item.get("backdrop_path"),
        "first_air_date": item.get("first_air_date"),
        "genres": _genre_names(item, genre_map),
    })


def _persist_discovered_movie(item: Dict, genre_map: Dict[int, str]) -> None:
    """Movie twin of _persist_discovered_show (caches into tmdb_movies)."""
    if not item.get("id"):
        return
    upsert_movie_info({
        "tmdb_id": item.get("id"),
        "title": item.get("title") or item.get("original_title"),
        "overview": item.get("overview"),
        "poster_path": item.get("poster_path"),
        "backdrop_path": item.get("backdrop_path"),
        "release_date": item.get("release_date"),
        "genres": _genre_names(item, genre_map),
    })
