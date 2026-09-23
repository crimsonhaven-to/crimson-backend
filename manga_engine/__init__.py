"""The reading surface: AniList discovery plus chapters resolved in the browser,
or server-side by an injected provider (see ``provider.py``)."""

from manga_engine.routes import router as manga_router

__all__ = ["manga_router"]
