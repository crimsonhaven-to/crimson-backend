"""The local ``tmdb_shows`` and ``tmdb_movies`` rows: the fallback when TMDB is
down and the candidate pool the recommend engine scores. Written from full
fetches and, lazily, from every search and discover result the TMDB fetchers see.

Every function here is synchronous; async callers go through ``asyncio.to_thread``.
"""

import json
import logging
from typing import Dict, List, Optional

from core.db_pool import get_connection

logger = logging.getLogger("crimson.store")


def get_first_anilist_ids(tmdb_ids: List[int]) -> Dict[int, int]:
    """Each tmdb_id's lowest mapped season's anilist_id. Unmapped ids are absent."""
    if not tmdb_ids:
        return {}
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT tmdb_id, anilist_id
                   FROM tmdb_seasons
                   WHERE tmdb_id = ANY(%s)
                   ORDER BY tmdb_id, season_number""",
                (list(tmdb_ids),),
            ).fetchall()
    except Exception as e:
        logger.error(f"Database error in get_first_anilist_ids: {e}")
        return {}
    out: Dict[int, int] = {}
    for r in rows:
        out.setdefault(r["tmdb_id"], r["anilist_id"])
    return out


def _genres_json(genres: Optional[List[str]]) -> Optional[str]:
    # Null rather than "[]", so the COALESCE in the upserts keeps stored genres
    # when a caller (the degraded path, a discover result) has none to offer.
    return json.dumps(genres) if genres else None


def upsert_shows(shows: List[Dict]) -> None:
    rows = [
        (
            s["tmdb_id"], s.get("title"), s.get("overview"), s.get("poster_path"),
            s.get("backdrop_path"), s.get("first_air_date"), _genres_json(s.get("genres")),
            s.get("popularity"),
        )
        for s in shows if s.get("tmdb_id")
    ]
    if not rows:
        return
    try:
        with get_connection() as conn:
            conn.cursor().executemany(
                """
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
                """,
                rows,
            )
    except Exception as e:
        logger.error(f"Database error in upsert_shows: {e}")


def upsert_movies(movies: List[Dict]) -> None:
    rows = [
        (
            m["tmdb_id"], m.get("title"), m.get("overview"), m.get("poster_path"),
            m.get("backdrop_path"), m.get("release_date"), _genres_json(m.get("genres")),
            m.get("runtime"), m.get("vote_average"), m.get("popularity"), m.get("status"),
            m.get("original_title"),
        )
        for m in movies if m.get("tmdb_id")
    ]
    if not rows:
        return
    try:
        with get_connection() as conn:
            # runtime, status and the rest come only from the full /movie/{id}
            # fetch, so a discover upsert must not blank them.
            conn.cursor().executemany(
                """
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
                """,
                rows,
            )
    except Exception as e:
        logger.error(f"Database error in upsert_movies: {e}")


def _genre_names(item: Dict, genre_map: Dict[int, str]) -> List[str]:
    return [genre_map[g] for g in (item.get("genre_ids") or []) if g in genre_map]


def persist_discovered_shows(items: List[Dict], genre_map: Dict[int, str]) -> None:
    """Keep TMDB discover/search results so they can become recommendation
    candidates without anyone opening their overview."""
    upsert_shows([
        {
            "tmdb_id": item.get("id"),
            "title": item.get("name") or item.get("original_name"),
            "overview": item.get("overview"),
            "poster_path": item.get("poster_path"),
            "backdrop_path": item.get("backdrop_path"),
            "first_air_date": item.get("first_air_date"),
            "genres": _genre_names(item, genre_map),
            "popularity": item.get("popularity"),
        }
        for item in items
    ])


def persist_discovered_movies(items: List[Dict], genre_map: Dict[int, str]) -> None:
    upsert_movies([
        {
            "tmdb_id": item.get("id"),
            "title": item.get("title") or item.get("original_title"),
            "overview": item.get("overview"),
            "poster_path": item.get("poster_path"),
            "backdrop_path": item.get("backdrop_path"),
            "release_date": item.get("release_date"),
            "genres": _genre_names(item, genre_map),
            "popularity": item.get("popularity"),
        }
        for item in items
    ])
