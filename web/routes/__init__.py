"""The backend's ``APIRouter``s, grouped by concern.

api.py includes every router in ``all_routers``, order preserved so the few
overlapping path patterns still match as they should.
"""

from web.routes.system import router as system_router
from web.routes.discovery import router as discovery_router
from web.routes.metadata import router as metadata_router
from web.routes.watch import router as watch_router
from web.routes.proxies import router as proxies_router
from web.routes.local_library import router as local_library_router
from web.routes.metrics import router as metrics_router

# Order matters: watch_router comes before metadata_router so the literal-segment
# /watch/movie/{tmdb_id} registers ahead of the 2-segment
# /watch/{anilist_id}/{episode_number} compatibility route.
all_routers = [
    system_router,
    discovery_router,
    watch_router,
    metadata_router,
    proxies_router,
    # Its paths don't overlap any of the above, so this position isn't
    # load-bearing.
    local_library_router,
    # One schema-hidden route enforcing its own token/admin auth. No overlap.
    metrics_router,
]

__all__ = ["all_routers"]
