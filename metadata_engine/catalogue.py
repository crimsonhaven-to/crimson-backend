"""Reads over the mapping and catalogue tables.

Every function degrades to empty on a database error, so a DB hiccup reads as
"not found" to the caller rather than a 500 on a metadata page.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from core.db_pool import get_connection

from .tmdb import tmdb_img

logger = logging.getLogger("crimson.catalogue")


def decode_genres(raw) -> List[str]:
    """A ``genres`` JSON column as a list. Rows synced before genres existed are
    null, and anything malformed degrades to empty too."""
    try:
        return json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []


def _one(sql: str, params) -> Optional[dict]:
    try:
        with get_connection() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"Catalogue query failed: {e}")
        return None


def _all(sql: str, params=()) -> List[dict]:
    try:
        with get_connection() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception as e:
        logger.error(f"Catalogue query failed: {e}")
        return []


# --- mapping --------------------------------------------------------------------
def get_anilist_id(tmdb_id: int, season_number: int) -> Optional[int]:
    row = _one(
        "SELECT anilist_id FROM tmdb_seasons WHERE tmdb_id = %s AND season_number = %s",
        (tmdb_id, season_number),
    )
    return row["anilist_id"] if row else None


def get_tmdb_season(anilist_id: int) -> Optional[Tuple[int, Optional[int]]]:
    """(tmdb_id, season_number) for an anilist_id. A special, OVA or movie has no
    numbered season and comes back as (tmdb_id, None)."""
    row = _one(
        "SELECT tmdb_id, season_number FROM tmdb_seasons WHERE anilist_id = %s", (anilist_id,)
    )
    if row:
        return row["tmdb_id"], row["season_number"]
    row = _one("SELECT tmdb_id FROM tmdb_extras WHERE anilist_id = %s LIMIT 1", (anilist_id,))
    return (row["tmdb_id"], None) if row else None


def get_extra_movie_id(anilist_id: int) -> Optional[int]:
    """The TMDB movie id of an extra that is a film in its own right, which plays
    through the movie pipeline rather than a season and episode it has no page for."""
    row = _one(
        "SELECT tmdb_movie_id FROM tmdb_extras "
        "WHERE anilist_id = %s AND tmdb_movie_id IS NOT NULL LIMIT 1",
        (anilist_id,),
    )
    return row["tmdb_movie_id"] if row else None


def get_show_seasons(tmdb_id: int) -> List[Dict]:
    return _all(
        """
        SELECT s.season_number, s.anilist_id, e.title_romaji, e.title_english, e.anime_type
        FROM tmdb_seasons s
        JOIN anime_entries e ON s.anilist_id = e.anilist_id
        WHERE s.tmdb_id = %s
        ORDER BY s.season_number
        """,
        (tmdb_id,),
    )


def get_show_extras(tmdb_id: int) -> List[Dict]:
    """Specials, OVAs and movies tied to a show. ``tmdb_movie_id`` is set only on
    films TMDB tracks as movies, which the client plays through the movie path."""
    return _all(
        """
        SELECT x.anilist_id, x.anime_type, x.tmdb_movie_id,
               e.title_romaji, e.title_english, e.start_year
        FROM tmdb_extras x
        LEFT JOIN anime_entries e ON x.anilist_id = e.anilist_id
        WHERE x.tmdb_id = %s
        ORDER BY e.start_year, x.anilist_id
        """,
        (tmdb_id,),
    )


def get_anime_genres(anilist_id: int) -> List[str]:
    row = _one("SELECT genres FROM anime_entries WHERE anilist_id = %s", (anilist_id,))
    return decode_genres(row["genres"]) if row else []


def get_show_info(tmdb_id: int) -> Dict:
    return _one("SELECT * FROM tmdb_shows WHERE tmdb_id = %s", (tmdb_id,)) or {}


def get_movie_info(tmdb_id: int) -> Dict:
    return _one("SELECT * FROM tmdb_movies WHERE tmdb_id = %s", (tmdb_id,)) or {}


def mapping_stats() -> dict:
    """Row counts and the last sync, all null when the schema is not there yet."""
    row = _one(
        """
        SELECT (SELECT COUNT(*) FROM anime_entries) AS anime_entries,
               (SELECT COUNT(*) FROM tmdb_seasons)  AS tmdb_seasons,
               (SELECT COUNT(*) FROM tmdb_extras)   AS tmdb_extras,
               (SELECT COUNT(*) FROM tmdb_shows)    AS tmdb_shows,
               (SELECT COUNT(*) FROM tmdb_movies)   AS tmdb_movies,
               (SELECT COUNT(*) FROM api_cache)     AS api_cache,
               (SELECT value FROM sync_meta WHERE key = 'etag') AS mapping_etag,
               (SELECT MAX(last_synced) FROM anime_entries) AS last_synced
        """,
        (),
    )
    return row or dict.fromkeys(
        (
            "anime_entries",
            "tmdb_seasons",
            "tmdb_extras",
            "tmdb_shows",
            "tmdb_movies",
            "api_cache",
            "mapping_etag",
            "last_synced",
        )
    )


# --- browse lists -----------------------------------------------------------------
def get_catalogue_items() -> List[Dict]:
    """Every mapped anime, sorted by title, with the ids the client navigates by:
    tmdb_id and season_number for a TV season, or tmdb_movie_id for a film that
    is its own TMDB entity. Posters come from lazily filled tables, so many are
    null."""
    try:
        with get_connection() as conn:
            season_map: Dict[int, Tuple[int, int]] = {}
            for r in conn.execute("SELECT anilist_id, tmdb_id, season_number FROM tmdb_seasons"):
                season_map.setdefault(r["anilist_id"], (r["tmdb_id"], r["season_number"]))
            extra_map: Dict[int, int] = {}
            for r in conn.execute("SELECT anilist_id, tmdb_id FROM tmdb_extras"):
                extra_map.setdefault(r["anilist_id"], r["tmdb_id"])
            show_posters = {
                r["tmdb_id"]: r["poster_path"]
                for r in conn.execute("SELECT tmdb_id, poster_path FROM tmdb_shows")
            }
            # Movie ids are their own number space, overlapping show ids.
            movie_posters = {
                r["tmdb_id"]: r["poster_path"]
                for r in conn.execute("SELECT tmdb_id, poster_path FROM tmdb_movies")
            }
            entries = conn.execute(
                """SELECT anilist_id, title_romaji, title_english, title_native,
                          anime_type, start_year, genres, tmdb_movie_id
                   FROM anime_entries"""
            ).fetchall()
    except Exception as e:
        logger.error(f"Catalogue query failed: {e}")
        return []

    items: List[Dict] = []
    for entry in entries:
        title = entry["title_english"] or entry["title_romaji"] or entry["title_native"]
        if not title:
            continue
        aid = entry["anilist_id"]
        tmdb_id: Optional[int] = None
        season_number: Optional[int] = None
        if aid in season_map:
            tmdb_id, season_number = season_map[aid]
        elif aid in extra_map:
            tmdb_id = extra_map[aid]
        movie_id = entry["tmdb_movie_id"]
        if tmdb_id is None and movie_id is None:
            continue  # nothing the client could open
        poster_path = (
            show_posters.get(tmdb_id) if tmdb_id is not None else movie_posters.get(movie_id)
        )
        items.append(
            {
                "anilist_id": aid,
                "title": title,
                "title_romaji": entry["title_romaji"],
                "title_english": entry["title_english"],
                "category": entry["anime_type"] or "UNKNOWN",
                "genres": decode_genres(entry["genres"]),
                "year": entry["start_year"],
                "tmdb_id": tmdb_id,
                "season_number": season_number,
                "tmdb_movie_id": movie_id,
                "poster": tmdb_img(poster_path) if poster_path else None,
            }
        )

    items.sort(key=lambda x: (x["title"] or "").lower())
    return items


def _popular_first(item: Dict) -> tuple:
    popularity = item["popularity"]
    year = item["year"] or ""
    return (
        -(popularity if isinstance(popularity, (int, float)) else float("-inf")),
        -(int(year) if year.isdigit() else 0),
        (item["title"] or "").lower(),
    )


def _tmdb_cards(rows: List[dict], kind: str, date_column: str) -> List[Dict]:
    items = []
    for r in rows:
        if not r["title"]:
            continue
        date = r[date_column] or ""
        card = {
            "tmdb_id": r["tmdb_id"],
            "anilist_id": None,
            "kind": kind,
            "title": r["title"],
            "poster": tmdb_img(r["poster_path"]) if r["poster_path"] else None,
            "year": date[:4] if date else None,
            "popularity": r["popularity"],
            "genres": decode_genres(r["genres"]),
        }
        if "vote_average" in r:
            card["vote_average"] = r["vote_average"]
        items.append(card)
    items.sort(key=_popular_first)
    return items


def get_shows_catalogue_items() -> List[Dict]:
    """Non-anime TV from the local table, popular first, so the grid leads with
    popular titles even before a full backfill."""
    rows = _all(
        "SELECT tmdb_id, title, poster_path, first_air_date, genres, popularity FROM tmdb_shows"
    )
    return _tmdb_cards(rows, "show", "first_air_date")


def get_movies_catalogue_items() -> List[Dict]:
    rows = _all(
        """SELECT tmdb_id, title, poster_path, release_date, genres, popularity, vote_average
           FROM tmdb_movies"""
    )
    return _tmdb_cards(rows, "movie", "release_date")


# --- local anime search ---------------------------------------------------------------
# A query matching this many rows is too vague to autocomplete usefully, and the
# cap keeps a one-letter query from sorting the whole catalogue.
_SEARCH_SCAN_CAP = 400
# Backslash is LIKE's default escape in Postgres, and the pattern travels as a
# bound parameter, so it reaches the server unmangled.
_LIKE_ESCAPES = str.maketrans({"\\": r"\\", "%": r"\%", "_": r"\_"})


def _escape_like(value: str) -> str:
    """Without this, "_" matches every one-character title and "%" the whole
    catalogue: a denial of service dressed as a typo."""
    return value.translate(_LIKE_ESCAPES)


def search_anime_entries(query: str, limit: int = 10) -> List[Dict]:
    """Anime whose titles contain ``query``, best match first, in the same shape
    as ``fetch_tmdb_search_results`` so the two merge invisibly.

    Ranked with a plain CASE rather than pg_trgm's similarity(), so the query is
    identical with or without migration 003's trigram index."""
    needle = (query or "").strip()
    if not needle:
        return []
    escaped = _escape_like(needle)
    rows = _all(
        """
        SELECT e.anilist_id,
               e.title_romaji,
               e.title_english,
               e.title_native,
               e.start_year,
               e.tmdb_movie_id,
               s.tmdb_id      AS season_tmdb_id,
               s.season_number,
               x.tmdb_id      AS extra_tmdb_id,
               sh.poster_path AS show_poster,
               mv.poster_path AS movie_poster
        FROM anime_entries e
        -- One row per entry, deterministically: an id can map to several seasons.
        LEFT JOIN LATERAL (
            SELECT tmdb_id, season_number
            FROM tmdb_seasons
            WHERE anilist_id = e.anilist_id
            ORDER BY tmdb_id, season_number
            LIMIT 1
        ) s ON TRUE
        LEFT JOIN LATERAL (
            SELECT tmdb_id
            FROM tmdb_extras
            WHERE anilist_id = e.anilist_id
            ORDER BY tmdb_id
            LIMIT 1
        ) x ON TRUE
        LEFT JOIN tmdb_shows  sh ON sh.tmdb_id = COALESCE(s.tmdb_id, x.tmdb_id)
        LEFT JOIN tmdb_movies mv ON mv.tmdb_id = e.tmdb_movie_id
        WHERE (e.title_romaji  ILIKE %(contains)s
            OR e.title_english ILIKE %(contains)s
            OR e.title_native  ILIKE %(contains)s)
          AND (s.tmdb_id IS NOT NULL
            OR x.tmdb_id IS NOT NULL
            OR e.tmdb_movie_id IS NOT NULL)
        ORDER BY
            CASE
                WHEN LOWER(e.title_english) = %(exact)s
                  OR LOWER(e.title_romaji)  = %(exact)s THEN 0
                WHEN e.title_english ILIKE %(prefix)s
                  OR e.title_romaji  ILIKE %(prefix)s   THEN 1
                WHEN e.title_english ILIKE %(contains)s
                  OR e.title_romaji  ILIKE %(contains)s THEN 2
                -- A native-title match on a Latin query is usually incidental.
                ELSE 3
            END,
            e.start_year DESC NULLS LAST,
            e.anilist_id
        LIMIT %(cap)s
        """,
        {
            "contains": f"%{escaped}%",
            "prefix": f"{escaped}%",
            "exact": needle.lower(),
            "cap": min(max(limit, 1), _SEARCH_SCAN_CAP),
        },
    )

    items: List[Dict] = []
    for r in rows:
        title = r["title_english"] or r["title_romaji"] or r["title_native"]
        if not title:
            continue
        tmdb_id = r["season_tmdb_id"] or r["extra_tmdb_id"]
        poster_path = r["show_poster"] if tmdb_id else r["movie_poster"]
        items.append(
            {
                "title": title,
                "tmdb_id": tmdb_id,
                "anilist_id": r["anilist_id"],
                "poster": tmdb_img(poster_path) if poster_path else None,
                "year": str(r["start_year"]) if r["start_year"] else None,
                # TMDB results carry a score and local rows do not; the client reads
                # it only when sorting a hub, never on a suggestion.
                "vote_average": None,
            }
        )
    return items
