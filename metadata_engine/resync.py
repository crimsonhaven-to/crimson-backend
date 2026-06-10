"""One-shot forced mapping resync — runnable inside a container via `docker exec`.

Rebuilds the AniList<->TMDB mapping tables (anime_entries / tmdb_seasons /
tmdb_extras) from the Fribb dataset, *bypassing* the ETag up-to-date check. Use
it after a schema change that adds a column to backfill (e.g. the genres
column), or any time you want to force-refresh the catalogue — without waiting
for Fribb's upstream ETag to move, and without exposing an admin endpoint.

It runs as its own short-lived process with its own pooled DB connection (reads
DATABASE_URL / TMDB_API_KEY from the container env, exactly like the app). The
rebuild is a single transaction, so the live api / api-sync replicas keep
serving the previous snapshot until it commits (MVCC) — no downtime, no
"database is locked".

Usage (on the Swarm node running the single api-sync task):

    cid=$(docker ps -q -f name=crimson-api_api-sync)
    docker exec "$cid" python -m metadata_engine.resync

(Replace the stack name ``crimson-api`` if you deployed under a different one.)
The command blocks until the rebuild finishes (the Fribb download + AniList
metadata fetch can take a few minutes) and exits non-zero if it fails.
"""
import asyncio
import sys

from metadata_engine.db_handler import MappingDatabaseEngine


def main() -> int:
    engine = MappingDatabaseEngine()
    try:
        asyncio.run(engine.sync_database_async(force=True))
    except Exception as e:  # surface a non-zero exit for `docker exec` callers
        print(f"[resync] Forced resync failed: {e}", file=sys.stderr)
        return 1
    print("[resync] Forced resync complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
