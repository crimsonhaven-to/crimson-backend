"""What a song or show card in a message stores.

The card is a snapshot taken when the message is sent, so it reads the same after
the song leaves the library or the show's metadata changes. A song is checked
against its share token, which only a member with the song in their own library
can mint, so a message cannot conjure a song nobody shared.
"""

from typing import Optional

from music_engine import links
from music_engine.db import STATUS_READY, store as music_store

from .schemas import SongAttachment, TitleAttachment


class UnknownSong(Exception):
    pass


def _poster(url: Optional[str]) -> Optional[str]:
    """Only https, so a crafted card cannot point an <img> at javascript: or at
    plain http on a page served over TLS."""
    return url if url and url.startswith("https://") else None


def snapshot(attachment: SongAttachment | TitleAttachment) -> dict:
    if isinstance(attachment, TitleAttachment):
        return {
            "kind": "title",
            "media": attachment.media,
            "id": attachment.id,
            "title": attachment.title.strip(),
            "poster": _poster(attachment.poster),
        }
    track_id = links.shared_track_id(attachment.token)
    track = music_store.get_track(track_id) if track_id else None
    if not track or track["status"] != STATUS_READY:
        raise UnknownSong()
    return {
        "kind": "song",
        "token": attachment.token,
        "title": track["title"],
        "artists": track["artists"],
        "album": track["album"],
        "duration_ms": track["duration_ms"],
        "cover": _poster(track["cover_url"]),
    }
