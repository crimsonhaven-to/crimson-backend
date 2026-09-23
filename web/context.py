"""Process-wide singletons shared by the routes, the pipeline and the lifespan.

Here rather than in api.py so every module reaches the *same* instances without a
circular import. Nothing opens a connection or touches the network at import
time; the stores are schema-init'd in api.py's lifespan.
"""

from core.db_pool import get_pool
from metadata_engine.db_handler import MappingDatabaseEngine
from local_engine.db import LocalSourceStore
from cache_engine.db import CacheStore
from download_engine.db import DownloadStore
from telemetry_engine import TelemetryStore

# Mapping/metadata engine, stored in the shared pool.
db_engine = MappingDatabaseEngine()

# The "Local" direct-play source. Schema-init'd in lifespan; the scraper and
# resolver read enabled roots via their own store, whose cache is class-wide.
local_source_store = LocalSourceStore()

# Downloads played episodes to a NAS target and replays them as a named source.
# Schema-init'd and started in lifespan.
cache_store = CacheStore()

# aria2-backed downloads landing under a download-enabled source's
# crimson-downloads/ dir. Only the RUN_DOWNLOAD_WORKER replica polls.
download_store = DownloadStore()

# Client beacons aggregated daily, restoring the source-success visibility lost
# when resolving moved client-side.
telemetry_store = TelemetryStore()


def get_db_connection():
    """Borrow a pooled PostgreSQL connection as a context manager.

    Commits on a clean exit, rolls back on error, and returns the connection to
    the pool. FastAPI serves these synchronous calls from its thread pool and the
    pool is thread-safe, so many workers can share one database concurrently.
    """
    return get_pool().connection()
