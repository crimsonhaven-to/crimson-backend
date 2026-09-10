"""
Canonical wire contracts shared with the frontend and crimson-sources.

The single source of truth for the /watch NDJSON protocol that
``stream_watch_response()`` produces and crimson-client consumes. It used to be a
prose comment duplicated across three repos, where silent drift broke playback
for everyone, so it lives here as:

  * builder functions the producer calls, so the shape exists in exactly one
    place in the backend, and
  * a JSON Schema the test suite validates those builders against, exported to
    ``contracts/watch_ndjson.schema.json`` for the frontend to vendor.

Regenerate the exported schema after changing anything here::

    python -m core.contracts

The protocol is a discriminated union on ``type``. The producer emits one
``meta`` line, then either an ``unaired`` line or zero or more ``stream`` lines,
and always a final ``done`` line.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

# Bump when the protocol changes in a way the client must know about. Mirrored in
# the schema's ``$id`` so a vendored copy can assert which version it targets.
WATCH_PROTOCOL_VERSION = 1


# --- builders (the producer's single source of truth) ----------------------
def build_meta_line(
    *,
    tmdb_id: int,
    season_number: Optional[int],
    episode_number: Optional[int],
    anilist_id: Optional[int],
    title: Optional[str],
) -> Dict[str, Any]:
    """First line of every /watch stream. Flushed immediately so the player can
    render its header before any source lands."""
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
    """Replaces every ``stream`` line when the episode is dated in the future, so
    the client can render a "not yet aired" state."""
    return {
        "type": "unaired",
        "air_date": air_date,
        "title": title,
        "season_number": season_number,
        "episode_number": episode_number,
    }


def build_stream_line(stream: Dict[str, Any]) -> Dict[str, Any]:
    """One resolved playable source, projecting the internal resolver dict onto
    the wire shape. Note ``type`` becomes ``streamType``, and ``cacheTicket``
    appears only on cacheable streams while server-side caching is on."""
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
    """Final line of every /watch stream: how many ``stream`` lines preceded it."""
    return {"type": "done", "count": count}


# --- JSON Schema (validated against the builders in tests) ------------------
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
    """Absolute path of the committed, generated schema file."""
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "contracts",
        "watch_ndjson.schema.json",
    )


def schema_json() -> str:
    """The schema serialized exactly as written to disk, so diffs stay stable."""
    return json.dumps(WATCH_NDJSON_SCHEMA, indent=2, ensure_ascii=False) + "\n"


def _write_schema() -> None:
    path = export_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(schema_json())
    print(f"wrote {path}")


# For callers that build a whole stream's worth of lines (tests, docs).
def all_event_types() -> List[str]:
    return ["meta", "unaired", "stream", "done"]


if __name__ == "__main__":
    _write_schema()
