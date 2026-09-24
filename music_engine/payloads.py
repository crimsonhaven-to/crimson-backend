"""A track as the player and the admin library see it: the signed links to its
audio and cover, from the CDN copy when there is one."""

from typing import Optional

from . import cdn, links


def track_payload(row: dict, base: str) -> dict:
    ready = row["status"] == "ready"
    stream_url: Optional[str]
    cover_url: Optional[str]
    if ready and row.get("mirrored_at") and cdn.enabled():
        stream_url = cdn.signed_url(row["rel_path"])
        cover_url = cdn.signed_url(row["cover_path"]) if row["cover_path"] else row["cover_url"]
    else:
        stream_url = base + links.signed_path(links.STREAM, row["id"]) if ready else None
        cover_url = (
            base + links.signed_path(links.ART, row["id"]) if row["cover_path"] else row["cover_url"]
        )
    return {
        "id": row["id"],
        "spotify_id": row["spotify_id"],
        "title": row["title"],
        "artists": row["artists"],
        "album": row["album"],
        "duration_ms": row["duration_ms"],
        "status": row["status"],
        "error": row["error"],
        "removed_upstream": bool(row.get("removed_upstream")),
        "cover_url": cover_url,
        "stream_url": stream_url,
    }
