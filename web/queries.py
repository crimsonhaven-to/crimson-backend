"""Read-only queries over the mapping and catalogue tables.

These import only ``web.context.get_db_connection`` and the TMDB image helper, so
they carry no app coupling and every route module can share them.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from metadata_engine.tmdb import _tmdb_img

from web.context import get_db_connection

logger = logging.getLogger("crimson.queries")


def get_anilist_id(tmdb_id: int, season_number: int) -> Optional[int]:
    """The mapped AniList id for a TMDB id and season."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT anilist_id FROM tmdb_seasons WHERE tmdb_id = %s AND season_number = %s",
                (tmdb_id, season_number)
            )
            row = cursor.fetchone()
            return row["anilist_id"] if row else None
    except Exception as e:
        logger.error(f"Database error in get_anilist_id: {e}")
        return None


def get_tmdb_season(anilist_id: int) -> Optional[Tuple[int, Optional[int]]]:
    """Reverse lookup: (tmdb_id, season_number) for an anilist_id.

    Falls back to tmdb_extras, in which case season_number is None.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tmdb_id, season_number FROM tmdb_seasons WHERE anilist_id = %s",
                (anilist_id,)
            )
            row = cursor.fetchone()
            if row:
                return (row["tmdb_id"], row["season_number"])

            # Not a numbered season, so perhaps a special, OVA or movie.
            cursor.execute(
                "SELECT tmdb_id FROM tmdb_extras WHERE anilist_id = %s LIMIT 1",
                (anilist_id,)
            )
            row = cursor.fetchone()
            return (row["tmdb_id"], None) if row else None
    except Exception as e:
        logger.error(f"Database error in get_tmdb_season: {e}")
        return None


def get_anime_genres(anilist_id: int) -> List[str]:
    """Genres for one anime, from the same local table the catalogue uses.

    A single-row read, so /overview ships genres without an external API call.
    Returns [] for non-anime or unknown ids.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT genres FROM anime_entries WHERE anilist_id = %s",
                (anilist_id,)
            )
            row = cursor.fetchone()
        if not row or not row["genres"]:
            return []
        return json.loads(row["genres"])
    except (TypeError, ValueError):
        return []
    except Exception as e:
        logger.error(f"Database error in get_anime_genres: {e}")
        return []


def get_show_seasons(tmdb_id: int) -> List[Dict]:
    """Every mapped season of a show, with its ids and titles."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT s.season_number, s.anilist_id, e.title_romaji, e.title_english, e.anime_type
                FROM tmdb_seasons s
                JOIN anime_entries e ON s.anilist_id = e.anilist_id
                WHERE s.tmdb_id = %s
                ORDER BY s.season_number
            """, (tmdb_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_show_seasons: {e}")
        return []


def get_anime_entry(anilist_id: Optional[int]) -> Dict:
    """The anime_entries row for an anilist_id."""
    if not anilist_id:
        return {}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM anime_entries WHERE anilist_id = %s", (anilist_id,))
            row = cursor.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Database error in get_anime_entry: {e}")
        return {}


def get_show_extras(tmdb_id: int) -> List[Dict]:
    """The specials, OVAs and movies tied to a show.

    ``tmdb_movie_id`` is set only on films TMDB tracks as standalone movies, and
    is the frontend's signal to route them through the movie watch path rather
    than the show's season/episode one.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT x.anilist_id, x.anime_type, x.tmdb_movie_id,
                       e.title_romaji, e.title_english, e.start_year
                FROM tmdb_extras x
                LEFT JOIN anime_entries e ON x.anilist_id = e.anilist_id
                WHERE x.tmdb_id = %s
                ORDER BY e.start_year, x.anilist_id
            """, (tmdb_id,))
            return [dict(row) for row in cursor.fetchall()]
    except Exception as e:
        logger.error(f"Database error in get_show_extras: {e}")
        return []


def get_extra_movie_id(anilist_id: int) -> Optional[int]:
    """The TMDB movie id of an extra that is a film in its own right, else None.

    Tells the watch path to serve the extra through the movie pipeline rather
    than build a season/episode URL a film has no page for.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT tmdb_movie_id FROM tmdb_extras "
                "WHERE anilist_id = %s AND tmdb_movie_id IS NOT NULL LIMIT 1",
                (anilist_id,)
            )
            row = cursor.fetchone()
            return row["tmdb_movie_id"] if row else None
    except Exception as e:
        logger.error(f"Database error in get_extra_movie_id: {e}")
        return None


def get_show_info(tmdb_id: int) -> Dict:
    """The cached tmdb_shows row for a show."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tmdb_shows WHERE tmdb_id = %s", (tmdb_id,))
            row = cursor.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Database error in get_show_info: {e}")
        return {}


def get_movie_info(tmdb_id: int) -> Dict:
    """The cached tmdb_movies row, keyed by TMDB movie id."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tmdb_movies WHERE tmdb_id = %s", (tmdb_id,))
            row = cursor.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        logger.error(f"Database error in get_movie_info: {e}")
        return {}


def get_catalogue_items() -> List[Dict]:
    """The full anime catalogue from the local DB, with no external calls.

    One row per mapped AniList entry, carrying its category and the ids the
    frontend navigates by: anilist_id for /seasons, tmdb_id plus season_number
    for /info and /watch, or tmdb_movie_id for a film that is its own TMDB
    entity. Posters come from the lazily populated tables, so they are often
    null. Sorted by title.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # For real TV seasons.
            cursor.execute("SELECT anilist_id, tmdb_id, season_number FROM tmdb_seasons")
            season_map: Dict[int, Tuple[int, int]] = {}
            for r in cursor.fetchall():
                season_map.setdefault(r["anilist_id"], (r["tmdb_id"], r["season_number"]))

            # For extras: specials, OVAs and movies.
            cursor.execute("SELECT anilist_id, tmdb_id FROM tmdb_extras")
            extra_map: Dict[int, int] = {}
            for r in cursor.fetchall():
                extra_map.setdefault(r["anilist_id"], r["tmdb_id"])

            # Sparse: only shows that have been opened once.
            cursor.execute("SELECT tmdb_id, poster_path FROM tmdb_shows")
            posters: Dict[int, Optional[str]] = {r["tmdb_id"]: r["poster_path"] for r in cursor.fetchall()}

            # Anime films keyed by their own TMDB movie id. That is a separate id
            # space from tmdb_shows, and the numbers overlap, hence its own map.
            cursor.execute("SELECT tmdb_id, poster_path FROM tmdb_movies")
            movie_posters: Dict[int, Optional[str]] = {r["tmdb_id"]: r["poster_path"] for r in cursor.fetchall()}

            cursor.execute(
                """SELECT anilist_id, title_romaji, title_english, title_native,
                          anime_type, start_year, genres, tmdb_movie_id
                   FROM anime_entries"""
            )
            entries = cursor.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_catalogue_items: {e}")
        return []

    items: List[Dict] = []
    for e in entries:
        title = e["title_english"] or e["title_romaji"] or e["title_native"]
        if not title:
            continue  # AniList titles never resolved, so useless in a list
        aid = e["anilist_id"]
        tmdb_id: Optional[int] = None
        season_number: Optional[int] = None
        if aid in season_map:
            tmdb_id, season_number = season_map[aid]
        elif aid in extra_map:
            tmdb_id = extra_map[aid]
        # A film TMDB tracks in its own right has no show to sit under, but is
        # still listable and playable through its own movie id.
        movie_id = e["tmdb_movie_id"]
        if tmdb_id is None and movie_id is None:
            continue  # unreachable: nothing the frontend could open
        poster_path = (posters.get(tmdb_id) if tmdb_id is not None
                       else movie_posters.get(movie_id))
        # A JSON-encoded list, null for entries synced before genres existed.
        try:
            genres = json.loads(e["genres"]) if e["genres"] else []
        except (TypeError, ValueError):
            genres = []
        items.append({
            "anilist_id": aid,
            "title": title,
            "title_romaji": e["title_romaji"],
            "title_english": e["title_english"],
            "category": e["anime_type"] or "UNKNOWN",
            "genres": genres,
            "year": e["start_year"],
            "tmdb_id": tmdb_id,
            "season_number": season_number,
            "tmdb_movie_id": movie_id,
            "poster": _tmdb_img(poster_path) if poster_path else None,
        })

    items.sort(key=lambda x: (x["title"] or "").lower())
    return items


def _decode_genres(raw) -> List[str]:
    """Decode a ``genres`` JSON column to a list. Null and malformed values both
    degrade to ``[]``, mirroring the decode in get_catalogue_items."""
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


def get_shows_catalogue_items() -> List[Dict]:
    """The non-anime TV catalogue from the local table, with no live TMDB.

    One poster card per row, tagged ``kind: 'show'`` and keyed by tmdb_id so the
    frontend routes it through the TMDB-keyed pages. Ordered by popularity, then
    year, then title, so the grid leads with popular titles even before a full
    backfill.

    Rows are populated lazily by search, trending and overviews, and in bulk by
    the nightly TMDB-discover backfill.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT tmdb_id, title, poster_path, first_air_date, genres, popularity
                   FROM tmdb_shows"""
            )
            rows = cursor.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_shows_catalogue_items: {e}")
        return []

    items: List[Dict] = []
    for r in rows:
        title = r["title"]
        if not title:
            continue  # a row with no title is useless in a browse list
        first_air = r["first_air_date"] or ""
        items.append({
            "tmdb_id": r["tmdb_id"],
            "anilist_id": None,
            "kind": "show",
            "title": title,
            "poster": _tmdb_img(r["poster_path"]) if r["poster_path"] else None,
            "year": first_air[:4] if first_air else None,
            "popularity": r["popularity"],
            "genres": _decode_genres(r["genres"]),
        })

    # Popular first, with the -inf sentinel putting NULLs last, then newest.
    items.sort(key=lambda x: (
        -(x["popularity"] if isinstance(x["popularity"], (int, float)) else float("-inf")),
        -(int(x["year"]) if (x["year"] or "").isdigit() else 0),
        (x["title"] or "").lower(),
    ))
    return items


def get_movies_catalogue_items() -> List[Dict]:
    """The general-movie catalogue, the twin of get_shows_catalogue_items. It also
    carries ``vote_average``, since movies have a rating column and shows do not.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """SELECT tmdb_id, title, poster_path, release_date, genres,
                          popularity, vote_average
                   FROM tmdb_movies"""
            )
            rows = cursor.fetchall()
    except Exception as e:
        logger.error(f"Database error in get_movies_catalogue_items: {e}")
        return []

    items: List[Dict] = []
    for r in rows:
        title = r["title"]
        if not title:
            continue
        release = r["release_date"] or ""
        items.append({
            "tmdb_id": r["tmdb_id"],
            "anilist_id": None,
            "kind": "movie",
            "title": title,
            "poster": _tmdb_img(r["poster_path"]) if r["poster_path"] else None,
            "year": release[:4] if release else None,
            "popularity": r["popularity"],
            "vote_average": r["vote_average"],
            "genres": _decode_genres(r["genres"]),
        })

    items.sort(key=lambda x: (
        -(x["popularity"] if isinstance(x["popularity"], (int, float)) else float("-inf")),
        -(int(x["year"]) if (x["year"] or "").isdigit() else 0),
        (x["title"] or "").lower(),
    ))
    return items
