"""OpenSubtitles-backed external subtitle tracks for the in-app player.

See ``routes.py`` for the two endpoints and ``service.py`` for the OpenSubtitles
client (search + quota-aware download + SRT→VTT conversion)."""

from .routes import router
from .service import service

__all__ = ["router", "service"]
