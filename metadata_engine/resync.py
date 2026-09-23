"""Forced mapping resync from the command line, for when the rebuild should not
wait for Fribb's ETag to move (a new column to backfill, say):

    docker exec "$(docker ps -q -f name=crimson-api_api-sync)" python -m metadata_engine.resync

Blocks until the rebuild finishes, which can take minutes, and exits non-zero on
failure. The rebuild is one transaction, so live replicas keep serving the
previous snapshot until it commits. Prints rather than logs: nothing configures
logging in this short-lived process.
"""

import asyncio
import sys

from core.db_pool import close_pool
from metadata_engine.mapping_sync import engine


def main() -> int:
    try:
        outcome = asyncio.run(engine.sync_database_async(force=True))
    except Exception as e:
        print(f"[resync] Forced resync failed: {e}", file=sys.stderr)
        return 1
    finally:
        # The pool's worker threads would otherwise keep the process alive.
        close_pool()
    if outcome != "synced":
        print(f"[resync] Forced resync did not complete: {outcome}", file=sys.stderr)
        return 1
    print("[resync] Forced resync complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
