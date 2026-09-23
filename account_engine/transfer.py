"""Watchlist export and import files.

The export is one row per title, newest first, each carrying its ``list_name``
so every list fits in one file. The import accepts anything the export
produces: our JSON, a bare JSON array, or CSV with or without the BOM.
"""

import csv
import io
import json
from datetime import datetime
from typing import List, Optional, Tuple

from .library import favorite_item_key

# Internal keys (user_id, item_key) stay out; list_name leads so a spreadsheet
# sorted on it groups by list.
EXPORT_FIELDS = (
    "list_name", "title", "media_type", "tmdb_id", "anilist_id",
    "season_number", "poster", "added_at",
)


def export_json(rows: List[dict], now: datetime) -> str:
    return json.dumps(
        {
            "exported_at": now.isoformat(),
            "count": len(rows),
            "watchlists": [{k: r.get(k) for k in EXPORT_FIELDS} for r in rows],
        },
        ensure_ascii=False,
        indent=2,
    )


def export_csv(rows: List[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k) for k in EXPORT_FIELDS})
    # Excel reads UTF-8 correctly only after a BOM; without it non-ASCII titles
    # come out mangled.
    return "﻿" + buf.getvalue()


def parse_export(raw: bytes) -> List[dict]:
    """Row dicts from an uploaded file, format sniffed from the first character.
    Raises on anything unreadable."""
    text = raw.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    if text[:1] in "[{":
        data = json.loads(text)
        if isinstance(data, dict):
            rows = data.get("watchlists") or data.get("favorites") or []
        elif isinstance(data, list):
            rows = data
        else:
            rows = []
        return [r for r in rows if isinstance(r, dict)]
    return [dict(r) for r in csv.DictReader(io.StringIO(text))]


def _coerce_int(val) -> Optional[int]:
    """CSV gives everything as strings, so accept '5', '5.0', ints and blanks."""
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _clean_str(val) -> Optional[str]:
    if val is None:
        return None
    return str(val).strip() or None


def favorites_from_rows(rows: List[dict]) -> Tuple[List[tuple], int]:
    """``(list_name, favorite)`` pairs ready to upsert, and how many rows had no
    id and were dropped."""
    favs: List[tuple] = []
    skipped_no_id = 0
    for r in rows:
        tmdb_id = _coerce_int(r.get("tmdb_id"))
        anilist_id = _coerce_int(r.get("anilist_id"))
        if tmdb_id is None and anilist_id is None:
            skipped_no_id += 1
            continue
        media_type = _clean_str(r.get("media_type"))
        favs.append((
            (_clean_str(r.get("list_name")) or "favorites")[:100],
            {
                "item_key": favorite_item_key(tmdb_id, anilist_id, media_type),
                "tmdb_id": tmdb_id,
                "anilist_id": anilist_id,
                "season_number": _coerce_int(r.get("season_number")),
                "media_type": media_type,
                "title": _clean_str(r.get("title")),
                "poster": _clean_str(r.get("poster")),
            },
        ))
    return favs, skipped_no_id
