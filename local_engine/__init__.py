# local_engine — admin-managed "Local" media source (a direct-play-only,
# Jellyfin-lite that streams browser-playable files straight off a mounted
# directory / NAS share). The DB store lives in .db; the filesystem helpers
# (token encoding, the per-request safety check, path inspection + mount
# discovery for the admin dashboard) live in .fs. The actual scraper/resolver
# that plug into the watch pipeline live under scrapers/ and resolvers/ and
# import from here.
from .db import LocalSourceStore

__all__ = ["LocalSourceStore"]
