"""One-shot forced mapping resync, runnable in a container via `docker exec`.

Rebuilds the mapping tables from the Fribb dataset, bypassing the ETag
up-to-date check. Use it after a schema change that adds a column to backfill,
or to force-refresh the catalogue without waiting for Fribb's ETag to move and
without exposing an admin endpoint.

Runs as its own short-lived process reading the container env, exactly like the
app. The rebuild is a single transaction, so the live replicas keep serving the
previous snapshot until it commits: no downtime.

Usage, on the node running the single api-sync task:

    cid=$(docker ps -q -f name=crimson-api_api-sync)
    docker exec "$cid" python -m metadata_engine.resync

Blocks until the rebuild finishes, which can take a few minutes, and exits
non-zero if it fails.
"""
import asyncio
import sys

from core.db_pool import close_pool
from metadata_engine.db_handler import MappingDatabaseEngine


def main() -> int:
    engine = MappingDatabaseEngine()
    try:
        asyncio.run(engine.sync_database_async(force=True))
    except Exception as e:  # non-zero exit for `docker exec` callers
        print(f"[resync] Forced resync failed: {e}", file=sys.stderr)
        return 1
    finally:
        # Stop the pool's worker threads so this short-lived process exits
        # promptly instead of lingering.
        close_pool()
    print("[resync] Forced resync complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
