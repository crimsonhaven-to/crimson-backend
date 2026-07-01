"""The backend's ``APIRouter``s, grouped by concern.

``api.py`` includes every router in ``all_routers`` (order preserved, so the few
overlapping path patterns — e.g. ``/watch/movie/{id}`` before the 2-segment
``/watch/{anilist}/{ep}`` — still match the way they did as one flat module).
"""

from web.routes.system import router as system_router
from web.routes.discovery import router as discovery_router
from web.routes.metadata import router as metadata_router
from web.routes.watch import router as watch_router
from web.routes.proxies import router as proxies_router

# Order matters: watch_router must be included before metadata_router so the
# literal-segment /watch/movie/{tmdb_id} is registered ahead of the 2-segment
# /watch/{anilist_id}/{episode_number} compatibility route (both live in
# watch_router already, but keeping watch ahead of metadata preserves the exact
# global registration order the single-file api.py had).
all_routers = [
    system_router,
    discovery_router,
    watch_router,
    metadata_router,
    proxies_router,
]

__all__ = ["all_routers"]
