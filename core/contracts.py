"""The /watch NDJSON protocol shared with crimson-client and crimson-sources.

Silent drift here breaks playback in every client, so the shape lives in one
place: the builders the producer calls, and a JSON Schema the tests validate
them against, exported to ``contracts/watch_ndjson.schema.json`` for the
frontend to vendor. Regenerate it after any change with ``python -m core.contracts``.

A stream is one ``meta`` line, then either one ``unaired`` line or zero or more
``stream`` lines, then a final ``done`` line.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

# Bump when the protocol changes in a way the client must know about. Mirrored in
# the schema's ``$id`` so a vendored copy can assert which version it targets.
WATCH_PROTOCOL_VERSION = 1


def build_meta_line(
    *,
    tmdb_id: int,
    season_number: Optional[int],
    episode_number: Optional[int],
    anilist_id: Optional[int],
    title: Optional[str],
) -> Dict[str, Any]:
    """Flushed immediately so the player can render its header before any
    source lands."""
    return {
        "type": "meta",
        "success": True,
        "tmdb_id": tmdb_id,
        "season_number": season_number,
        "episode_number": episode_number,
        "anilist_id": anilist_id,
        "title": title,
    }


def build_unaired_line(
    *,
    air_date: Optional[str],
    title: Optional[str],
    season_number: Optional[int],
    episode_number: Optional[int],
) -> Dict[str, Any]:
    """Sent instead of any ``stream`` line when the episode airs in the future."""
    return {
        "type": "unaired",
        "air_date": air_date,
        "title": title,
        "season_number": season_number,
        "episode_number": episode_number,
    }


def build_stream_line(stream: Dict[str, Any]) -> Dict[str, Any]:
    """Project a resolver stream dict onto the wire. ``type`` becomes
    ``streamType``; ``cacheTicket`` appears only while server-side caching is on."""
    line: Dict[str, Any] = {
        "type": "stream",
        "source": stream["source"],
        "streamType": stream["type"],
        "url": stream["url"],
        "language": stream.get("language"),
        "subtitles": stream.get("subtitles"),
    }
    if stream.get("cacheTicket"):
        line["cacheTicket"] = stream["cacheTicket"]
    return line


def build_done_line(count: int) -> Dict[str, Any]:
    """``count`` is the number of ``stream`` lines sent."""
    return {"type": "done", "count": count}


_SUBTITLE_SCHEMA = {
    "type": "object",
    "required": ["url", "lang"],
    "properties": {
        "url": {"type": "string"},
        "lang": {"type": "string"},
        "label": {"type": "string"},
    },
}

WATCH_NDJSON_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": f"https://crimsonhaven.to/contracts/watch_ndjson/v{WATCH_PROTOCOL_VERSION}",
    "title": "Crimson /watch NDJSON line",
    "description": (
        "One line of the /watch line-delimited JSON stream. Discriminated on "
        "`type`. Generated from core/contracts.py; do not edit by hand."
    ),
    "oneOf": [
        {
            "type": "object",
            "required": ["type", "success", "tmdb_id", "season_number",
                         "episode_number", "anilist_id", "title"],
            "additionalProperties": False,
            "properties": {
                "type": {"const": "meta"},
                "success": {"type": "boolean"},
                "tmdb_id": {"type": "integer"},
                "season_number": {"type": ["integer", "null"]},
                "episode_number": {"type": ["integer", "null"]},
                "anilist_id": {"type": ["integer", "null"]},
                "title": {"type": ["string", "null"]},
            },
        },
        {
            "type": "object",
            "required": ["type", "air_date", "title", "season_number", "episode_number"],
            "additionalProperties": False,
            "properties": {
                "type": {"const": "unaired"},
                "air_date": {"type": ["string", "null"]},
                "title": {"type": ["string", "null"]},
                "season_number": {"type": ["integer", "null"]},
                "episode_number": {"type": ["integer", "null"]},
            },
        },
        {
            "type": "object",
            "required": ["type", "source", "streamType", "url", "language", "subtitles"],
            "additionalProperties": False,
            "properties": {
                "type": {"const": "stream"},
                "source": {"type": "string"},
                "streamType": {"enum": ["hls", "mp4", "iframe"]},
                "url": {"type": "string"},
                "language": {"type": ["string", "null"]},
                "subtitles": {
                    "type": ["array", "null"],
                    "items": _SUBTITLE_SCHEMA,
                },
                "cacheTicket": {"type": "string"},
            },
        },
        {
            "type": "object",
            "required": ["type", "count"],
            "additionalProperties": False,
            "properties": {
                "type": {"const": "done"},
                "count": {"type": "integer", "minimum": 0},
            },
        },
    ],
}


def export_path() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "contracts",
        "watch_ndjson.schema.json",
    )


def schema_json() -> str:
    return json.dumps(WATCH_NDJSON_SCHEMA, indent=2, ensure_ascii=False) + "\n"


def _write_schema() -> None:
    path = export_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(schema_json())
    print(f"wrote {path}")


if __name__ == "__main__":
    _write_schema()
