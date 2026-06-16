"""
Changelog engine — exposes the repo's GitHub Releases as a public /changelog.

Public surface:
    from changelog_engine import router, service
api.py mounts ``router``, warms ``service`` at startup, and schedules a periodic
``service.refresh()`` — mirroring how the other engines are wired.
"""

from .routes import router, service
from .service import ChangelogService

__all__ = ["router", "service", "ChangelogService"]
