"""A track or playlist as the player and the admin library see it: the signed
links to its audio and cover, from the CDN copy when there is one, and the file's
size, which the player needs to tell a member what a download will take on the
device."""

from typing import Optional
from urllib.parse import quote

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
        "genres": row.get("genres") or [],
        "duration_ms": row["duration_ms"],
        "file_size": row["file_size"] if ready else None,
        "status": row["status"],
        "error": row["error"],
        "removed_upstream": bool(row.get("removed_upstream")),
        "cover_url": cover_url,
        "stream_url": stream_url,
    }


def playlist_payload(row: dict, base: str) -> dict:
    """A playlist with its custom cover, when it has one, in place of the one
    its import brought. The upload time rides in the link so a new cover is not
    served from the day-long cache of the old one."""
    custom = row.get("custom_cover_at")
    payload = {**row, "custom_cover": bool(custom)}
    if custom:
        link = links.signed_path(links.PLAYLIST_ART, row["id"])
        payload["cover_url"] = f"{base}{link}&v={quote(custom)}"
    return payload
