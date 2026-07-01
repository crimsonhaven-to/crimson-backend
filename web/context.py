"""Process-wide singletons shared by the routes, the pipeline and the lifespan.

These used to be module-level globals in ``api.py``. They live here now so the
route/handler modules and ``api.py``'s lifespan can all reach the *same* instances
without importing ``api.py`` (which would be circular). Nothing here opens a
connection or touches the network at import time — the stores are schema-init'd in
``api.py``'s lifespan exactly as before.
"""

from core.config import Config
from core.db_pool import get_pool
from metadata_engine.db_handler import MappingDatabaseEngine
from local_engine.db import LocalSourceStore
from cache_engine.db import CacheStore
from telemetry_engine import TelemetryStore

# Mapping/metadata engine (storage is the shared PostgreSQL pool; see db_pool).
db_engine = MappingDatabaseEngine(tmdb_api_key=Config.TMDB_API_KEY)

# Admin-managed local media sources (the "Local" direct-play source). The store is
# schema-init'd in lifespan; the scraper/resolver read the enabled roots directly
# via their own LocalSourceStore (the enabled-roots cache is class-wide).
local_source_store = LocalSourceStore()

# Server-side video cache (downloads played episodes to a NAS target and replays
# them as a named source). Schema-init'd + download manager started in lifespan;
# the scraper/resolver/proxy read enabled targets via their own CacheStore.
cache_store = CacheStore()

# Anonymous per-source resolve telemetry (client beacons -> daily aggregates).
# Restores the source-success visibility lost when resolving moved client-side.
telemetry_store = TelemetryStore()


def get_db_connection():
    """Borrow a pooled PostgreSQL connection as a context manager.

    Returns the pool's connection context manager, so the existing
    ``with get_db_connection() as conn:`` call sites keep working unchanged: the
    transaction commits on a clean exit (rolls back on error) and the connection
    returns to the pool. FastAPI serves these synchronous DB calls from its thread
    pool, and the pool is thread-safe, so many workers (and replicas) can share the
    same external database concurrently.
    """
    return get_pool().connection()
