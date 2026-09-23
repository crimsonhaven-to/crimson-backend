"""A year of watching, aggregated per account.

``watch_events`` (migration 006, one row per item per day) is the honest source,
but it only exists from the day it shipped. For any earlier span of the year the
numbers fall back to ``watch_progress.updated_at``, which is when a row was last
touched, not when it was watched, and which cannot see a rewatch. That fallback
is never hidden: the payload then carries ``approximate: true`` and
``events_since``.

Everything is computed in Python from one bounded row set per source (a heavy
viewer is a few thousand rows a year), because the counting rules are the part
worth getting right and are far easier to read and argue with as plain loops.
"""

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from core.db_pool import get_connection

logger = logging.getLogger(__name__)

# Real UTC offsets. A wider one would also move the local day further than the
# one day of query padding in build() covers.
MIN_OFFSET_MINUTES = -12 * 60
MAX_OFFSET_MINUTES = 14 * 60

# Genres are stored as a JSON list in a TEXT column on three different tables.
_GENRE_SOURCES = (
    ("anime", "SELECT anilist_id AS id, genres FROM anime_entries WHERE anilist_id = ANY(%s)"),
    ("show", "SELECT tmdb_id AS id, genres FROM tmdb_shows WHERE tmdb_id = ANY(%s)"),
    ("movie", "SELECT tmdb_id AS id, genres FROM tmdb_movies WHERE tmdb_id = ANY(%s)"),
)

TOP_GENRES = 8


def _surface(row: dict) -> str:
    """Which part of the site this row was watched on: an explicit media_type
    wins, otherwise an AniList id means anime."""
    media_type = row.get("media_type")
    if media_type in ("manga", "movie", "local"):
        return media_type
    return "anime" if row.get("anilist_id") is not None else "show"


# Progress item keys end in ":s{season}:e{episode}"; stripping that gives the
# show back for local media, the one surface with no AniList or TMDB id.
_EPISODE_SUFFIX = re.compile(r"(:s-?\d+)?(:e-?\d+)$")


def _show_key(entry: dict) -> str:
    """The title an entry belongs to, so twelve episodes are one show. TMDB
    numbers movies and shows independently, so a movie gets its own prefix."""
    if entry["surface"] == "manga":
        return entry["item_key"]
    if entry.get("anilist_id") is not None:
        return f"anilist:{entry['anilist_id']}"
    if entry["surface"] == "local":
        return _EPISODE_SUFFIX.sub("", entry["item_key"])
    if entry.get("tmdb_id") is not None:
        prefix = "movie" if entry["surface"] == "movie" else "tmdb"
        return f"{prefix}:{entry['tmdb_id']}"
    return entry["item_key"]


def _local_day(moment: datetime, offset_minutes: int) -> date:
    return (moment + timedelta(minutes=offset_minutes)).date()


def _load_events(conn, user_id: int, lo: date, hi: date) -> List[dict]:
    rows = conn.execute(
        """
        SELECT item_key, anilist_id, tmdb_id, media_type, title, seconds, first_seen_at
        FROM watch_events
        WHERE user_id = %s AND watched_on BETWEEN %s AND %s
        """,
        (user_id, lo, hi),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_progress(conn, user_id: int) -> List[dict]:
    rows = conn.execute(
        """
        SELECT item_key, anilist_id, tmdb_id, media_type, title,
               position_seconds AS seconds, updated_at
        FROM watch_progress
        WHERE user_id = %s
        """,
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _events_since(conn, user_id: int) -> Optional[datetime]:
    """The first moment this account has an event row for, or None. Anything
    before it came from last-touch timestamps."""
    row = conn.execute(
        "SELECT MIN(first_seen_at) AS since FROM watch_events WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    return row["since"] if row else None


def _parse_updated_at(value) -> Optional[datetime]:
    """watch_progress.updated_at is ISO-8601 TEXT; a naive value is UTC."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _genres_for(conn, items: List[dict]) -> Counter:
    """Genre counts over distinct titles, so a long show does not bury the rest.

    Local rows have no genres and are left out rather than counted as "unknown".
    Manga is left out because its AniList ids are from the manga id space, and
    looking one up in anime_entries would return another title's genres."""
    anime_ids, show_ids, movie_ids = set(), set(), set()
    for item in items:
        surface = item["surface"]
        if surface == "anime" and item["anilist_id"] is not None:
            anime_ids.add(item["anilist_id"])
        elif surface == "show" and item["tmdb_id"] is not None:
            show_ids.add(item["tmdb_id"])
        elif surface == "movie" and item["tmdb_id"] is not None:
            movie_ids.add(item["tmdb_id"])

    counts: Counter = Counter()
    for (kind, sql), ids in zip(_GENRE_SOURCES, (anime_ids, show_ids, movie_ids)):
        if not ids:
            continue
        for row in conn.execute(sql, (list(ids),)).fetchall():
            raw = row.get("genres")
            if not raw:
                continue
            try:
                names = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(names, list):
                counts.update(str(n) for n in names if n)
    return counts


def _longest_streak(days: List[date]) -> Tuple[int, Optional[str], Optional[str]]:
    """Longest run of consecutive days with anything watched."""
    if not days:
        return (0, None, None)
    ordered = sorted(set(days))
    best_len, best_start, best_end = 1, ordered[0], ordered[0]
    run_len, run_start = 1, ordered[0]
    for previous, current in zip(ordered, ordered[1:]):
        if current - previous == timedelta(days=1):
            run_len += 1
        else:
            run_len, run_start = 1, current
        if run_len > best_len:
            best_len, best_start, best_end = run_len, run_start, current
    return (best_len, best_start.isoformat(), best_end.isoformat())


def build(user_id: int, year: int, offset_minutes: int = 0) -> dict:
    """One account's year, in the viewer's own timezone."""
    first_day, last_day = date(year, 1, 1), date(year, 12, 31)
    # watched_on is a UTC date and the local day can fall either side of it, so
    # the window is widened by a day and the local date does the real filtering.
    pad = timedelta(days=1)

    with get_connection() as conn:
        since = _events_since(conn, user_id)
        events = _load_events(conn, user_id, first_day - pad, last_day + pad)

        # (item_key, local day) is the unit of "watched this, that day". A
        # progress row only fills a day no event covers.
        by_day: Dict[Tuple[str, date], dict] = {}
        for row in events:
            day = _local_day(row["first_seen_at"], offset_minutes)
            if not (first_day <= day <= last_day):
                continue
            by_day[(row["item_key"], day)] = {**row, "day": day, "exact": True}

        # With no events at all, the whole year is approximate.
        approximate_until = _local_day(since, offset_minutes) if since else last_day + pad
        approximate = approximate_until > first_day

        if approximate:
            for row in _load_progress(conn, user_id):
                touched = _parse_updated_at(row.get("updated_at"))
                if touched is None:
                    continue
                day = _local_day(touched, offset_minutes)
                if not (first_day <= day <= last_day) or day >= approximate_until:
                    continue
                by_day.setdefault((row["item_key"], day), {**row, "day": day, "exact": False})

        entries = list(by_day.values())
        for entry in entries:
            entry["surface"] = _surface(entry)
            entry["show"] = _show_key(entry)
        genres = _genres_for(conn, entries)

    # One figure per item, at the furthest position ever reached: each day
    # records the furthest point so far, so summing days would double count an
    # episode watched across two. This undercounts a rewatch but never overcounts.
    furthest: Dict[str, float] = {}
    surfaces: Dict[str, str] = {}
    shows_seen: Dict[str, str] = {}
    titles: Dict[str, str] = {}
    for entry in entries:
        key = entry["item_key"]
        furthest[key] = max(furthest.get(key, 0.0), float(entry.get("seconds") or 0.0))
        surfaces[key] = entry["surface"]
        shows_seen[key] = entry["show"]
        if entry.get("title"):
            titles.setdefault(shows_seen[key], entry["title"])

    per_show: Dict[str, float] = {}
    for key, seconds in furthest.items():
        show = shows_seen[key]
        per_show[show] = per_show.get(show, 0.0) + seconds

    days = [entry["day"] for entry in entries]
    per_day = Counter(days)
    busiest_day, busiest_count = (per_day.most_common(1) or [(None, 0)])[0]
    streak_days, streak_start, streak_end = _longest_streak(days)

    # Never summed into one headline number: a manga row is one per title (the
    # chapter rides in episode_number), not one per episode.
    by_surface = Counter(surfaces.values())
    shows = {
        surface: len({entry["show"] for entry in entries if entry["surface"] == surface})
        for surface in by_surface
    }

    ordered = sorted(entries, key=lambda e: (e["day"], e["item_key"]))
    return {
        "year": year,
        "offset_minutes": offset_minutes,
        "approximate": approximate,
        "events_since": since.isoformat() if since else None,
        "episodes": by_surface.get("anime", 0) + by_surface.get("show", 0)
        + by_surface.get("local", 0),
        "movies": by_surface.get("movie", 0),
        "manga_titles": by_surface.get("manga", 0),
        "by_surface": dict(by_surface),
        "distinct_titles": dict(shows),
        "hours": round(sum(furthest.values()) / 3600.0, 1),
        "active_days": len(per_day),
        "busiest_day": {
            "day": busiest_day.isoformat() if busiest_day else None,
            "items": busiest_count,
        },
        "longest_streak": {
            "days": streak_days, "from": streak_start, "to": streak_end,
        },
        "top_genres": [
            {"genre": name, "count": count} for name, count in genres.most_common(TOP_GENRES)
        ],
        "first_title": titles.get(ordered[0]["show"]) if ordered else None,
        "last_title": titles.get(ordered[-1]["show"]) if ordered else None,
        "top_titles": [
            {"title": titles[show], "minutes": round(seconds / 60.0)}
            for show, seconds in sorted(
                per_show.items(), key=lambda kv: kv[1], reverse=True
            )[:10] if show in titles
        ],
    }
