"""
Local store for discovered TMDB metadata: the non-anime shows and movies surface.

Reads and writes the ``tmdb_shows`` / ``tmdb_movies`` tables and the
``tmdb_seasons`` mapping, and lazily persists discover and search results so they
can later become recommendation candidates. The TMDB fetchers are the callers.
"""

import json
import logging
from typing import Dict, List

from core.db_pool import get_connection

logger = logging.getLogger("crimson.store")


def get_first_anilist_ids(tmdb_ids: List[int]) -> Dict[int, int]:
    """Map each tmdb_id to its lowest-numbered season's anilist_id, in one query.

    Replaces an N+1 that borrowed a pooled connection per search result. A tmdb_id
    with no mapped season is simply absent.
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
                out.setdefault(r["tmdb_id"], r["anilist_id"])  # first is lowest season
            return out
    except Exception as e:
        logger.error(f"Database error in get_first_anilist_ids: {e}")
        return {}


def upsert_show_info(show: Dict) -> None:
    """Persist show details fetched on demand, lazily populating tmdb_shows."""
    if not show.get("tmdb_id"):
        return
    try:
        # Only overwrite when the caller supplies genres, so a later refresh that
        # omits them, such as the degraded path, cannot blank what is stored.
        genres = show.get("genres")
        genres_json = json.dumps(genres) if genres else None
        def _write():
            with get_connection() as conn:
                conn.execute("""
                    INSERT INTO tmdb_shows
                        (tmdb_id, title, overview, poster_path, backdrop_path, first_air_date, genres, popularity, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (tmdb_id) DO UPDATE SET
                        title=EXCLUDED.title, overview=EXCLUDED.overview,
                        poster_path=EXCLUDED.poster_path, backdrop_path=EXCLUDED.backdrop_path,
                        first_air_date=EXCLUDED.first_air_date,
                        genres=COALESCE(EXCLUDED.genres, tmdb_shows.genres),
                        popularity=COALESCE(EXCLUDED.popularity, tmdb_shows.popularity),
                        last_updated=CURRENT_TIMESTAMP
                """, (
                    show.get("tmdb_id"),
                    show.get("title"),
                    show.get("overview"),
                    show.get("poster_path"),
                    show.get("backdrop_path"),
                    show.get("first_air_date"),
                    genres_json,
                    show.get("popularity"),
                ))
        _write()
    except Exception as e:
        logger.error(f"Database error in upsert_show_info: {e}")


def upsert_movie_info(movie: Dict) -> None:
    """Persist movie details fetched on demand, mirroring upsert_show_info. This
    is the TMDB-down fallback for the movie routes, which have no AniList entry to
    fall back on."""
    if not movie.get("tmdb_id"):
        return
    try:
        genres = movie.get("genres")
        genres_json = json.dumps(genres) if genres else None
        def _write():
            with get_connection() as conn:
                # These come only from the full /movie/{id} fetch, so a discover
                # upsert omits them and the COALESCE keeps any previously fetched
                # value rather than blanking it. Same guard as genres.
                conn.execute("""
                    INSERT INTO tmdb_movies
                        (tmdb_id, title, overview, poster_path, backdrop_path, release_date,
                         genres, runtime, vote_average, popularity, status, original_title, last_updated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (tmdb_id) DO UPDATE SET
                        title=EXCLUDED.title, overview=EXCLUDED.overview,
                        poster_path=EXCLUDED.poster_path, backdrop_path=EXCLUDED.backdrop_path,
                        release_date=EXCLUDED.release_date,
                        genres=COALESCE(EXCLUDED.genres, tmdb_movies.genres),
                        runtime=COALESCE(EXCLUDED.runtime, tmdb_movies.runtime),
                        vote_average=COALESCE(EXCLUDED.vote_average, tmdb_movies.vote_average),
                        popularity=COALESCE(EXCLUDED.popularity, tmdb_movies.popularity),
                        status=COALESCE(EXCLUDED.status, tmdb_movies.status),
                        original_title=COALESCE(EXCLUDED.original_title, tmdb_movies.original_title),
                        last_updated=CURRENT_TIMESTAMP
                """, (
                    movie.get("tmdb_id"),
                    movie.get("title"),
                    movie.get("overview"),
                    movie.get("poster_path"),
                    movie.get("backdrop_path"),
                    movie.get("release_date"),
                    genres_json,
                    movie.get("runtime"),
                    movie.get("vote_average"),
                    movie.get("popularity"),
                    movie.get("status"),
                    movie.get("original_title"),
                ))
        _write()
    except Exception as e:
        logger.error(f"Database error in upsert_movie_info: {e}")


def _genre_names(item: Dict, genre_map: Dict[int, str]) -> List[str]:
    """Resolve an item's genre_ids to names via ``genre_map``."""
    return [genre_map[g] for g in (item.get("genre_ids") or []) if g in genre_map]


def _persist_discovered_show(item: Dict, genre_map: Dict[int, str]) -> None:
    """Cache a discovered non-anime show, so it can become a recommendation
    candidate without a full overview open. Best-effort."""
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
        "popularity": item.get("popularity"),
    })


def _persist_discovered_movie(item: Dict, genre_map: Dict[int, str]) -> None:
    """The movie twin of _persist_discovered_show."""
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
        "popularity": item.get("popularity"),
    })
