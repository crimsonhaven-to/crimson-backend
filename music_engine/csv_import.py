"""Reading a playlist from a CSV export, the fallback when the Web API is out of
reach. Exportify is the expected source, but its columns changed over the years
and other exporters name them differently, so every field is looked up by a
list of names rather than a fixed position."""

from __future__ import annotations

import csv
import io
import re
from typing import Optional

from .provider import ImportedTrack

MAX_ROWS = 10_000

_COLUMNS = {
    "uri": ("track uri", "spotify uri", "spotify - id", "spotify id", "uri"),
    "title": ("track name", "name", "title", "track", "song"),
    "artists": ("artist name(s)", "artist names", "artist name", "artists", "artist"),
    "album": ("album name", "album"),
    "album_artist": ("album artist name(s)", "album artist"),
    "release_date": ("album release date", "release date"),
    "cover": ("album image url",),
    "disc": ("disc number",),
    "track_number": ("track number",),
    "duration": ("track duration (ms)", "duration (ms)", "duration_ms", "duration"),
    "isrc": ("isrc",),
    "added_at": ("added at",),
}

_SPOTIFY_ID = re.compile(r"(?:spotify:track:|open\.spotify\.com/track/)?([A-Za-z0-9]{22})\b")


class CsvImportError(ValueError):
    pass


def spotify_track_id(value: str) -> Optional[str]:
    match = _SPOTIFY_ID.search(value.strip()) if value else None
    return match.group(1) if match else None


def _split_artists(value: str) -> list[str]:
    """Newer exports separate artists with semicolons, older ones with commas."""
    separator = ";" if ";" in value else ","
    return [name.strip() for name in value.split(separator) if name.strip()]


def _duration_ms(value: str) -> int:
    value = value.strip()
    if ":" in value:
        minutes, _, seconds = value.partition(":")
        if minutes.isdigit() and seconds.isdigit():
            return (int(minutes) * 60 + int(seconds)) * 1000
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def _int_or_none(value: str) -> Optional[int]:
    return int(value) if value.strip().isdigit() else None


def parse_csv(text: str) -> list[ImportedTrack]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = {(name or "").strip().lower(): name for name in reader.fieldnames or []}
    column = {
        field: next((headers[alias] for alias in aliases if alias in headers), None)
        for field, aliases in _COLUMNS.items()
    }
    if not column["title"] or not column["artists"]:
        raise CsvImportError(
            "The file needs at least a track name and an artist column. "
            "Export the playlist with Exportify and upload that file as it is."
        )

    tracks: list[ImportedTrack] = []
    for index, row in enumerate(reader):
        if index >= MAX_ROWS:
            raise CsvImportError(f"The file has more than {MAX_ROWS} rows.")

        def cell(field: str) -> str:
            name = column[field]
            return (row.get(name) or "").strip() if name else ""

        title, artists = cell("title"), _split_artists(cell("artists"))
        if not title or not artists:
            continue
        album_artists = _split_artists(cell("album_artist"))
        tracks.append(
            ImportedTrack(
                title=title,
                artists=artists,
                spotify_id=spotify_track_id(cell("uri")),
                isrc=cell("isrc") or None,
                album=cell("album"),
                album_artist=album_artists[0] if album_artists else artists[0],
                track_number=_int_or_none(cell("track_number")),
                disc_number=_int_or_none(cell("disc")),
                release_date=cell("release_date") or None,
                duration_ms=_duration_ms(cell("duration")),
                cover_url=cell("cover") or None,
                added_at=cell("added_at") or None,
            )
        )
    if not tracks:
        raise CsvImportError("No row in the file had both a track name and an artist.")
    return tracks
