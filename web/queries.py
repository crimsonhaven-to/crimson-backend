"""Pure DB read helpers over the mapping/catalogue tables.

Read-only queries against the shared PostgreSQL pool, lifted verbatim from
``api.py``. They import only ``web.context.get_db_connection`` and the TMDB image
helper, so they carry no app coupling and can be shared by every route module.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from metadata_engine.tmdb import _tmdb_img

from web.context import get_db_connection

logger = logging.getLogger("crimson.queries")


def get_anilist_id(tmdb_id: int, season_number: int) -> Optional[int]:
    """Query mapped AniList ID from TMDB ID and season"""
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
    """
    Reverse lookup: returns (tmdb_id, season_number) for an anilist_id.

    Falls back to tmdb_extras (specials/OVAs/movies), in which case
    season_number is None.
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

            # Not a numbered season — maybe a special/OVA/movie.
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
    """Genres for a single anime, read from the local anime_entries DB.

    Same source the catalogue uses (genres is a JSON-encoded list, null for
    entries synced before the column existed). Cheap single-row read so the
    /overview endpoint can ship genres without an extra external API call.
    Returns [] for non-anime / unknown ids.
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
    """Returns all seasons with season_number, anilist_id, title_romaji, etc."""
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
    """Returns the anime_entries row (titles, type, year) for an anilist_id."""
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
    """Returns specials/OVAs/movies tied to a show (from tmdb_extras).

    ``tmdb_movie_id`` is set only on films TMDB tracks as a standalone movie; it
    is the frontend's routing signal, because those play through the movie watch
    path rather than the show's season/episode one. Null everywhere else.
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
    """The TMDB *movie* id of an extra, when it is a film TMDB tracks in its own
    right. None for a special/OVA/ONA (and for anything that isn't an extra).

    This is what tells the watch path to serve an extra through the movie
    pipeline instead of building a season/episode URL a film has no page for.
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
    """Gets show info from tmdb_shows table."""
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
    """Gets movie info from the tmdb_movies table (TMDB *movie* id keyed)."""
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
    """Build the full anime catalogue from the local DB only (no external calls).

    One row per AniList entry (every season / movie / OVA we have mapped), with
    its category (anime_type) and the ids the frontend needs to navigate
    (anilist_id for /seasons, tmdb_id + season_number for /info & /watch, or
    tmdb_movie_id for a film that is its own TMDB entity). Posters come from
    tmdb_shows / tmdb_movies where present (lazily populated, so often null)
    — we never hit TMDB here. Sorted by title.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # anilist_id -> (tmdb_id, season_number) for real TV seasons.
            cursor.execute("SELECT anilist_id, tmdb_id, season_number FROM tmdb_seasons")
            season_map: Dict[int, Tuple[int, int]] = {}
            for r in cursor.fetchall():
                season_map.setdefault(r["anilist_id"], (r["tmdb_id"], r["season_number"]))

            # anilist_id -> tmdb_id for extras (specials/OVAs/movies).
            cursor.execute("SELECT anilist_id, tmdb_id FROM tmdb_extras")
            extra_map: Dict[int, int] = {}
            for r in cursor.fetchall():
                extra_map.setdefault(r["anilist_id"], r["tmdb_id"])

            # tmdb_id -> poster_path (sparse; only shows that were opened once).
            cursor.execute("SELECT tmdb_id, poster_path FROM tmdb_shows")
            posters: Dict[int, Optional[str]] = {r["tmdb_id"]: r["poster_path"] for r in cursor.fetchall()}

            # The same, for anime films keyed by their own TMDB *movie* id. A
            # separate id space from tmdb_shows (the numbers overlap), hence a
            # separate map rather than more rows in `posters`.
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
            continue  # entry whose AniList titles never resolved — useless in a list
        aid = e["anilist_id"]
        tmdb_id: Optional[int] = None
        season_number: Optional[int] = None
        if aid in season_map:
            tmdb_id, season_number = season_map[aid]
        elif aid in extra_map:
            tmdb_id = extra_map[aid]
        # An anime film TMDB tracks in its own right has no show to sit under, so
        # it carries neither. It is still listable and playable through its own
        # movie id, which the frontend routes on (see tmdb_movie_id below).
        movie_id = e["tmdb_movie_id"]
        if tmdb_id is None and movie_id is None:
            continue  # unreachable entry: nothing the frontend could open
        poster_path = (posters.get(tmdb_id) if tmdb_id is not None
                       else movie_posters.get(movie_id))
        # genres is a JSON-encoded list (null for entries synced before genres
        # existed, or with no AniList genres); decode defensively to [].
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
    """Decode a tmdb_shows/tmdb_movies ``genres`` JSON string column to a list.

    Null (rows synced before genres, or with none) and malformed values both
    degrade to ``[]`` — mirrors the defensive decode in get_catalogue_items.
    """
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


def get_shows_catalogue_items() -> List[Dict]:
    """Full non-anime TV-show catalogue from the local tmdb_shows table (no live
    TMDB). One poster-card item per row, tagged ``kind: 'show'`` and keyed by
    tmdb_id so the frontend routes it through the TMDB-keyed show pages. Ordered
    by popularity (desc, NULLS LAST) then year then title, so the browse grid
    leads with the popular titles even before a full backfill.

    Rows are lazily populated by /search/shows, /trending/shows and show
    overviews, and bulk-populated by the nightly TMDB-discover backfill.
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
            continue  # a row with no resolved title is useless in a browse list
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

    # Popular first (NULLS LAST via the -inf sentinel), then newest, then title.
    items.sort(key=lambda x: (
        -(x["popularity"] if isinstance(x["popularity"], (int, float)) else float("-inf")),
        -(int(x["year"]) if (x["year"] or "").isdigit() else 0),
        (x["title"] or "").lower(),
    ))
    return items


def get_movies_catalogue_items() -> List[Dict]:
    """Full general-movie catalogue from the local tmdb_movies table (no live
    TMDB). The movie twin of get_shows_catalogue_items — additionally carries
    ``vote_average`` (movies have a rating column; shows do not). Ordered by
    popularity (desc, NULLS LAST) then year then title.
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
