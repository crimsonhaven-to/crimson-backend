"""The TMDB <-> AniList mapping tables, rebuilt from the Fribb dataset and
enriched with AniList titles. How the dataset becomes rows is in ``fribb``.

The rebuild replaces ``anime_entries``, ``tmdb_seasons`` and ``tmdb_extras`` in
one transaction, so readers keep the previous snapshot until it commits and a
failed rebuild leaves them intact. ``overrides.json`` is applied last and always
wins a season slot: the single maintenance lever for the long tail.

``init_db`` also creates tables other modules own (``tmdb_shows``,
``tmdb_movies``, ``metadata_backfill_jobs``, ``api_cache``) because startup runs
it first, before anything reads them.
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.clock import utc_now_iso
from core.db_pool import get_connection, lock_schema_init

from . import fribb

logger = logging.getLogger("crimson.mapping")


class MappingDatabaseEngine:
    MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
    ANILIST_API_URL = "https://graphql.anilist.co"
    OVERRIDES_PATH = Path(__file__).resolve().parent / "overrides.json"

    # AniList caps query complexity at 500 and the relations block costs ~22 per
    # aliased Media, so 25 aliases is rejected at 550 while 20 lands at ~440.
    # Raising this without re-measuring will 400 the whole chunk.
    ANILIST_CHUNK_SIZE = 20
    ANILIST_CHUNK_DELAY = 0.7

    def init_db(self):
        with get_connection() as conn:
            cursor = conn.cursor()
            lock_schema_init(conn)

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS anime_entries (
                    anilist_id    INTEGER PRIMARY KEY,
                    mal_id        INTEGER,
                    title_romaji  TEXT,
                    title_english TEXT,
                    title_native  TEXT,
                    anime_type    TEXT,
                    start_year    INTEGER,
                    genres        TEXT,
                    last_synced   TEXT,
                    tmdb_movie_id INTEGER
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tmdb_shows (
                    tmdb_id        INTEGER PRIMARY KEY,
                    title          TEXT,
                    overview       TEXT,
                    poster_path    TEXT,
                    backdrop_path  TEXT,
                    first_air_date TEXT,
                    genres         TEXT,
                    popularity     DOUBLE PRECISION,
                    last_updated   TIMESTAMP
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_shows_last_updated ON tmdb_shows(last_updated)")

            # Separate from tmdb_shows because the tv and movie id spaces overlap numerically.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tmdb_movies (
                    tmdb_id        INTEGER PRIMARY KEY,
                    title          TEXT,
                    overview       TEXT,
                    poster_path    TEXT,
                    backdrop_path  TEXT,
                    release_date   TEXT,
                    genres         TEXT,
                    runtime        INTEGER,
                    vote_average   DOUBLE PRECISION,
                    popularity     DOUBLE PRECISION,
                    status         TEXT,
                    original_title TEXT,
                    last_updated   TIMESTAMP
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_movies_last_updated ON tmdb_movies(last_updated)")

            # A queue, because the admin button runs on a serving replica that
            # cannot reach the portless sync container. Status walks
            # requested -> running -> done|failed.
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata_backfill_jobs (
                    id           SERIAL PRIMARY KEY,
                    status       TEXT NOT NULL DEFAULT 'requested',
                    pages        INTEGER,
                    requested_by TEXT,
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at   TIMESTAMP,
                    finished_at  TIMESTAMP,
                    shows        INTEGER,
                    movies       INTEGER,
                    error        TEXT
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_backfill_jobs_status_req "
                "ON metadata_backfill_jobs(status, requested_at)"
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tmdb_seasons (
                    tmdb_id       INTEGER,
                    season_number INTEGER,
                    anilist_id    INTEGER NOT NULL,
                    PRIMARY KEY (tmdb_id, season_number),
                    FOREIGN KEY (anilist_id) REFERENCES anime_entries(anilist_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS tmdb_extras (
                    tmdb_id       INTEGER,
                    anilist_id    INTEGER NOT NULL,
                    anime_type    TEXT,
                    tmdb_movie_id INTEGER,
                    PRIMARY KEY (tmdb_id, anilist_id),
                    FOREIGN KEY (anilist_id) REFERENCES anime_entries(anilist_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key     TEXT PRIMARY KEY,
                    response_json TEXT,
                    expires_at    TEXT
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_seasons_anilist ON tmdb_seasons(anilist_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_extras_anilist ON tmdb_extras(anilist_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_extras_show ON tmdb_extras(tmdb_id)")

        logger.info("Mapping schema ready")

    @staticmethod
    def _local_state() -> Tuple[Optional[str], int]:
        with get_connection() as conn:
            row = conn.execute(
                "SELECT (SELECT value FROM sync_meta WHERE key = 'etag') AS etag, "
                "(SELECT COUNT(*) FROM anime_entries) AS entries"
            ).fetchone()
        return row["etag"], row["entries"]

    async def _check_upstream(self, client: httpx.AsyncClient) -> Tuple[bool, Optional[str]]:
        """``(stale, upstream_etag)``.

        An empty local DB is always stale, which self-heals a wiped DB even when
        the upstream ETag has not moved. An unreachable upstream leaves a
        populated DB alone.
        """
        local_etag, entries = await asyncio.to_thread(self._local_state)
        try:
            response = await client.head(self.MAPPING_URL, follow_redirects=True)
        except Exception as e:
            logger.warning(f"Mapping update check failed: {e}")
            return entries == 0, None
        upstream_etag = response.headers.get("ETag")
        stale = entries == 0 or not local_etag or local_etag != upstream_etag
        return stale, upstream_etag

    async def _fetch_anilist_metadata_bulk(self, anilist_ids: List[int]) -> Dict[int, Dict]:
        """Titles, format and relations for many AniList ids, via aliased queries.

        Each relation edge's node brings its own format, title and year, so an
        extra found through it needs no second lookup. A failing chunk is logged
        and skipped: titles are best-effort and scrapers fetch them live anyway.
        """
        results: Dict[int, Dict] = {}
        chunk_size = self.ANILIST_CHUNK_SIZE

        # A client of its own: the scheduled sync runs on its own event loop, where the shared one cannot go.
        async with httpx.AsyncClient(timeout=30.0) as client:
            i = 0
            while i < len(anilist_ids):
                chunk = anilist_ids[i:i + chunk_size]
                query_parts = [
                    f"a{idx}: Media(id: {aid}, type: ANIME) {{ "
                    f"id idMal format genres title {{ romaji english native }} "
                    f"startDate {{ year }} "
                    f"relations {{ edges {{ relationType node {{ "
                    f"id format title {{ romaji english native }} "
                    f"startDate {{ year }} }} }} }} }}"
                    for idx, aid in enumerate(chunk)
                ]
                query = "query { " + " ".join(query_parts) + " }"

                try:
                    response = await client.post(self.ANILIST_API_URL, json={"query": query})
                except Exception as e:
                    logger.warning(f"AniList chunk request failed, skipping: {e}")
                    i += chunk_size
                    continue

                if response.status_code == 429:
                    retry_after = fribb.safe_int(response.headers.get("Retry-After")) or 60
                    logger.info(f"AniList rate limited, retrying the chunk in {retry_after}s")
                    await asyncio.sleep(retry_after)
                    continue

                try:
                    body = response.json()
                except Exception:
                    body = {}

                if response.status_code != 200:
                    # A non-200 may still carry partial data, used below. AniList's
                    # own messages are logged because a swallowed complexity 400
                    # otherwise drops the whole chunk with no clue why.
                    errs = body.get("errors") if isinstance(body, dict) else None
                    detail = "; ".join(str(e.get("message", e)) for e in errs[:3]) if errs else "no error detail"
                    logger.warning(f"AniList chunk returned {response.status_code}: {detail}")

                data = (body.get("data") if isinstance(body, dict) else None) or {}
                for media in data.values():
                    if media and media.get("id"):
                        results[media["id"]] = media

                i += chunk_size
                await asyncio.sleep(self.ANILIST_CHUNK_DELAY)

        return results

    def _apply_overrides(self, season_rows: List[fribb.SeasonRow], known_ids: set) -> List[fribb.SeasonRow]:
        overrides = fribb.load_overrides(self.OVERRIDES_PATH)
        if not overrides:
            return season_rows
        season_map = {(t, s): a for (t, s, a) in season_rows}
        for tmdb_id, seasons in overrides.items():
            for season, anilist_id in seasons.items():
                # An unknown id would fail the tmdb_seasons foreign key and roll
                # back the whole rebuild.
                if anilist_id not in known_ids:
                    logger.warning(f"Override skipped: AniList {anilist_id} is not in the dataset")
                    continue
                season_map[(tmdb_id, season)] = anilist_id
        logger.info(f"Applied overrides for {len(overrides)} show(s)")
        return [(t, s, a) for (t, s), a in season_map.items()]

    @staticmethod
    def _replace_tables(entry_rows: List[tuple], season_rows: List[fribb.SeasonRow],
                        extra_rows: List[fribb.ExtraRow], etag: str) -> int:
        """Swap in the new rows in one transaction; returns the catalogue cache rows purged."""
        with get_connection() as conn:
            cursor = conn.cursor()
            # Children before parents on delete, parents before children on insert, for the foreign keys.
            cursor.execute("DELETE FROM tmdb_seasons")
            cursor.execute("DELETE FROM tmdb_extras")
            cursor.execute("DELETE FROM anime_entries")
            cursor.executemany(
                """
                INSERT INTO anime_entries
                    (anilist_id, mal_id, title_romaji, title_english, title_native,
                     anime_type, start_year, genres, last_synced, tmdb_movie_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (anilist_id) DO NOTHING
                """,
                entry_rows,
            )
            cursor.executemany(
                "INSERT INTO tmdb_seasons (tmdb_id, season_number, anilist_id) VALUES (%s, %s, %s) "
                "ON CONFLICT (tmdb_id, season_number) DO NOTHING",
                season_rows,
            )
            cursor.executemany(
                "INSERT INTO tmdb_extras (tmdb_id, anilist_id, anime_type, tmdb_movie_id) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (tmdb_id, anilist_id) DO NOTHING",
                extra_rows,
            )
            cursor.execute(
                "INSERT INTO sync_meta (key, value) VALUES ('etag', %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (etag,),
            )
            # Otherwise the new titles stay masked by the cached /catalogue for up
            # to 6h. Same transaction, so the cache goes if and only if the rebuild
            # commits; the prefix match survives a cache-version bump. Each
            # replica's L1 is out of reach and expires on its own short TTL.
            cursor.execute("DELETE FROM api_cache WHERE cache_key LIKE %s", ("catalogue:%",))
            return cursor.rowcount or 0

    async def sync_database_async(self, force: bool = False) -> str:
        """Download the Fribb dataset and rebuild the mapping tables.

        ``force`` rebuilds even when the upstream ETag is unchanged, which is how
        a manual resync backfills after a schema change.

        Returns ``"up_to_date"``, ``"synced"``, ``"empty"`` (upstream parsed to
        no mappings, DB intact) or ``"failed"``.
        """
        await asyncio.to_thread(self.init_db)

        # A client of its own: the scheduled sync runs on its own event loop, where the shared one cannot go.
        async with httpx.AsyncClient(timeout=60.0) as client:
            logger.info("Mapping sync starting")
            stale, upstream_etag = await self._check_upstream(client)
            if stale:
                etag = upstream_etag or "force-empty-db"
            elif force:
                etag = upstream_etag or "forced-resync"
                logger.info("Forced resync: rebuilding despite an up-to-date ETag")
            else:
                logger.info("Mappings already up to date")
                return "up_to_date"

            logger.info("Downloading the Fribb anime-list")
            try:
                response = await client.get(self.MAPPING_URL, follow_redirects=True)
                response.raise_for_status()
                anime_data: List[Dict[str, Any]] = response.json()
            except Exception as e:
                logger.error(f"Fribb download failed: {e}")
                return "failed"

        groups, movie_id_by_anilist, orphan_movies = fribb.group_by_show(anime_data)
        season_rows, extra_rows = fribb.assign_seasons(groups, movie_id_by_anilist)
        if not season_rows and not extra_rows:
            logger.warning("No mappings parsed from the dataset, DB left intact")
            return "empty"

        # The Fribb type is the fallback when AniList has no format for an id.
        entry_type = {e["anilist_id"]: e["type"] for items in groups.values() for e in items}
        all_anilist_ids = set(orphan_movies) | set(entry_type)

        logger.info(f"Fetching AniList metadata for {len(all_anilist_ids)} ids")
        al_metadata = await self._fetch_anilist_metadata_bulk(sorted(all_anilist_ids))

        relation_rows = fribb.relation_extras(season_rows, extra_rows, al_metadata)
        for tmdb_id, anilist_id, node_format, node in relation_rows:
            extra_rows.append((tmdb_id, anilist_id, node_format, movie_id_by_anilist.get(anilist_id)))
            if anilist_id not in all_anilist_ids:
                all_anilist_ids.add(anilist_id)
                al_metadata.setdefault(anilist_id, node)
        if relation_rows:
            logger.info(f"AniList relations added {len(relation_rows)} extra(s)")

        now = utc_now_iso()
        entry_rows: List[tuple] = []
        for aid in all_anilist_ids:
            meta = al_metadata.get(aid, {})
            title = meta.get("title") or {}
            genres = meta.get("genres") or []
            entry_rows.append(
                (
                    aid,
                    meta.get("idMal"),
                    title.get("romaji"),
                    title.get("english"),
                    title.get("native"),
                    meta.get("format") or entry_type.get(aid),
                    (meta.get("startDate") or {}).get("year"),
                    json.dumps(genres) if genres else None,
                    now,
                    movie_id_by_anilist.get(aid),
                )
            )

        season_rows = self._apply_overrides(season_rows, all_anilist_ids)

        try:
            purged = await asyncio.to_thread(self._replace_tables, entry_rows, season_rows, extra_rows, etag)
        except Exception as e:
            logger.error(f"Mapping sync failed and rolled back: {e}")
            return "failed"

        logger.info(
            f"Mapping sync complete: entries={len(entry_rows)} seasons={len(season_rows)} "
            f"extras={len(extra_rows)} catalogue_cache_purged={purged}"
        )
        return "synced"


engine = MappingDatabaseEngine()
