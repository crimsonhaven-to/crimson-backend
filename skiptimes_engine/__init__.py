"""AniSkip-backed intro/outro skip timestamps for the anime player.

See ``routes.py`` for the ``/skiptimes`` endpoint and ``service.py`` for the
AniSkip client + cache."""

from .routes import router
from .service import service

__all__ = ["router", "service"]
