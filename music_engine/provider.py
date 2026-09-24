"""The seam an operator build fills with the music source.

The public backend imports playlists, stores the library and plays it back, but
never decides where audio comes from. An operator build drops a module into
this package declaring a module-level ``MUSIC_PROVIDER`` that satisfies
:class:`MusicProvider`. A base build has none: imports still work and the queue
waits until a provider exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Literal, Optional, Protocol


@dataclass
class ImportedTrack:
    """One playlist entry as an importer read it. Only ``title`` and ``artists``
    are guaranteed: the public fallback carries no album, a CSV may carry no id."""

    title: str
    artists: list[str]
    spotify_id: Optional[str] = None
    isrc: Optional[str] = None
    album: str = ""
    album_artist: str = ""
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    release_date: Optional[str] = None
    duration_ms: int = 0
    cover_url: Optional[str] = None
    added_at: Optional[str] = None


@dataclass
class ImportedPlaylist:
    name: str
    tracks: list[ImportedTrack]
    description: str = ""
    cover_url: Optional[str] = None
    snapshot_id: Optional[str] = None


@dataclass
class TrackQuery:
    """What the matcher needs to find a recording."""

    title: str
    artists: list[str]
    album: str
    duration_ms: int
    isrc: Optional[str]


@dataclass
class Candidate:
    """One recording the provider found, as the review picker shows it."""

    url: str
    title: str
    channel: str
    duration_ms: int
    thumbnail_url: str = ""
    score: float = 0.0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Match:
    """``auto`` carries the url to download, ``review`` asks a person to pick
    from ``candidates``, ``none`` found nothing worth showing."""

    kind: Literal["auto", "review", "none"]
    url: Optional[str] = None
    candidates: list[Candidate] = field(default_factory=list)


@dataclass
class Fetched:
    """A downloaded audio file in the work directory. ``album`` is what the
    source said, used only when the import had none."""

    path: str
    album: str = ""


class ProviderError(Exception):
    """A failure the provider can explain. ``retryable`` means trying again later
    may work (a rate limit, a network drop); otherwise it needs a person."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class MusicProvider(Protocol):
    async def match(self, track: TrackQuery) -> Match: ...

    async def search(self, query: str) -> list[Candidate]: ...

    async def fetch(self, url: str, work_dir: str) -> Fetched: ...

    async def public_playlist(self, playlist_id: str) -> ImportedPlaylist:
        """A public Spotify playlist read without an account, for users whose
        Spotify cannot use the Web API."""
        ...


@cache
def get_provider() -> Optional[MusicProvider]:
    """The injected provider, or ``None``. The overlay is fixed at process start."""
    import music_engine
    from core.private_sources import discover_provider

    try:
        return discover_provider(music_engine, "MUSIC_PROVIDER")
    except Exception:
        return None
