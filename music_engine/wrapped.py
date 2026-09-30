"""A year of listening, for Crimson Wrapped. The watching half lives in
account_engine/wrapped.py; this half counts music_listens, which is exact from
the day it shipped, so nothing here is ever approximate."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from . import listens_db

TOP_TRACKS = 5
TOP_ARTISTS = 5


def year_bounds(year: int, offset_minutes: int) -> tuple[datetime, datetime]:
    """The viewer's local year as a UTC range: local midnight is UTC midnight
    minus the offset."""
    shift = timedelta(minutes=offset_minutes)
    start = datetime(year, 1, 1, tzinfo=timezone.utc) - shift
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) - shift
    return start, end


def summarise(rows: list[dict], offset_minutes: int) -> dict:
    """Songs rank by how often they played, artists by how long, the way
    Spotify's own Wrapped ranks them. A song with two artists counts in full
    for each."""
    shift = timedelta(minutes=offset_minutes)
    plays: Counter[int] = Counter()
    track_seconds: defaultdict[int, float] = defaultdict(float)
    artist_seconds: defaultdict[str, float] = defaultdict(float)
    artist_plays: Counter[str] = Counter()
    tracks: dict[int, dict] = {}
    days = set()
    for row in rows:
        track_id = row["id"]
        seconds = float(row["seconds"] or 0.0)
        plays[track_id] += 1
        track_seconds[track_id] += seconds
        tracks[track_id] = row
        days.add((row["listened_at"] + shift).date())
        for artist in row.get("artists") or []:
            artist_seconds[artist] += seconds
            artist_plays[artist] += 1

    top_tracks = sorted(tracks, key=lambda t: (plays[t], track_seconds[t]), reverse=True)
    top_artists = sorted(artist_seconds, key=lambda a: artist_seconds[a], reverse=True)
    return {
        "plays": sum(plays.values()),
        "minutes": round(sum(track_seconds.values()) / 60.0),
        "songs": len(tracks),
        "artists": len(artist_seconds),
        "active_days": len(days),
        "top_tracks": [
            {"row": tracks[t], "plays": plays[t], "minutes": round(track_seconds[t] / 60.0)}
            for t in top_tracks[:TOP_TRACKS]
        ],
        "top_artists": [
            {"name": name, "plays": artist_plays[name],
             "minutes": round(artist_seconds[name] / 60.0)}
            for name in top_artists[:TOP_ARTISTS]
        ],
    }


def build(user_id: int, year: int, offset_minutes: int) -> dict:
    start, end = year_bounds(year, offset_minutes)
    return summarise(listens_db.between(user_id, start, end), offset_minutes)
