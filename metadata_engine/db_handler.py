"""
Metadata mapping engine.

Builds the local SQLite mapping between TMDB tv ids and AniList ids using the
Fribb anime-lists dataset (https://github.com/Fribb/anime-lists) enriched with
AniList titles.

Design
------
TMDB groups an anime as one "show" with numbered seasons. AniList gives every
cour/season/OVA/movie its own id. Fribb provides, per AniList entry, the parent
``themoviedb_id.tv`` plus ``season.tmdb`` (the TMDB season number that entry maps
to). We trust that field for real TV seasons and split everything else off:

* ``tmdb_seasons``  -> one AniList id per (tmdb_id, season_number) for season >= 1
* ``tmdb_extras``   -> every other entry tied to the show (specials/OVAs/movies,
                       plus the losers of a season collision) so nothing is lost

Collisions on a (tmdb_id, season_number) slot are resolved deterministically
(prefer a real TV entry, then the lowest AniList id). ``overrides.json`` is
applied last and always wins -- the single maintenance lever for the long tail.
"""

import asyncio
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

_THIS_DIR = Path(__file__).resolve().parent


class MappingDatabaseEngine:
    MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
    ANILIST_API_URL = "https://graphql.anilist.co"
    OVERRIDES_PATH = _THIS_DIR / "overrides.json"

    # AniList bulk-fetch tuning
    ANILIST_CHUNK_SIZE = 50
    ANILIST_CHUNK_DELAY = 0.7  # seconds between chunks (rate-limit friendly)

    def __init__(self, db_name: str = "anime_mappings.db", tmdb_api_key: Optional[str] = None):
        self.db_name = db_name
        self.tmdb_api_key = tmdb_api_key or os.getenv("TMDB_API_KEY")

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Best-effort conversion to int, returning None on failure."""
        if value is None:
            return None
        try:
            if isinstance(value, str) and "." in value:
                value = value.split(".")[0]
            return int(str(value).strip())
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _tmdb_tv_id(item: Dict[str, Any]) -> Optional[int]:
        """Extract the TMDB *tv* id from a Fribb entry (dict or scalar form)."""
        raw = item.get("themoviedb_id")
        if isinstance(raw, dict):
            return MappingDatabaseEngine._safe_int(raw.get("tv"))
        return MappingDatabaseEngine._safe_int(raw)

    @staticmethod
    def _tmdb_season(item: Dict[str, Any]) -> Optional[int]:
        """Extract the TMDB season number Fribb assigns to this entry."""
        season = item.get("season")
        if isinstance(season, dict):
            return MappingDatabaseEngine._safe_int(season.get("tmdb"))
        return MappingDatabaseEngine._safe_int(season)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _connect(self) -> sqlite3.Connection:
        # WAL + a generous busy timeout so the wholesale resync (BEGIN; DELETE;
        # bulk INSERT) can proceed while the API serves concurrent readers from
        # the same file without either side hitting "database is locked".
        conn = sqlite3.connect(self.db_name, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # ------------------------------------------------------------------ #
    # Schema
    # ------------------------------------------------------------------ #
    def init_db(self):
        """Create the schema (idempotent) and drop obsolete tables."""
        conn = self._connect()
        cursor = conn.cursor()

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
                last_synced   TIMESTAMP
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
                first_air_date TEXT
            )
            """
        )

        # One AniList id per real TMDB season (season_number >= 1).
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

        # Specials / OVAs / movies (and season-collision losers) tied to a show.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tmdb_extras (
                tmdb_id    INTEGER,
                anilist_id INTEGER NOT NULL,
                anime_type TEXT,
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
                expires_at    TIMESTAMP
            )
            """
        )

        # Indexes for the reverse (anilist -> tmdb) lookups.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_seasons_anilist ON tmdb_seasons(anilist_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_extras_anilist ON tmdb_extras(anilist_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_extras_show ON tmdb_extras(tmdb_id)")

        # Drop tables from earlier schema iterations if a persisted DB still has them.
        for legacy in ("show_groups", "group_members", "mappings", "season_groups"):
            cursor.execute(f"DROP TABLE IF EXISTS {legacy}")

        conn.commit()
        conn.close()
        print(f"[DB Engine] Schema ready at '{self.db_name}'.")

    def _entry_count(self) -> int:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM anime_entries")
            return cursor.fetchone()[0]
        except sqlite3.Error:
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    # Update detection
    # ------------------------------------------------------------------ #
    async def _check_needs_update(self, client: httpx.AsyncClient) -> Optional[str]:
        """
        Return the ETag to sync against, or None if already up-to-date.

        If the local DB is empty we always resync (self-heals a wiped DB even
        when the upstream ETag has not changed).
        """
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM sync_meta WHERE key = 'etag'")
            row = cursor.fetchone()
            current_etag = row[0] if row else None
        finally:
            conn.close()

        try:
            response = await client.head(self.MAPPING_URL, follow_redirects=True)
            new_etag = response.headers.get("ETag")
        except Exception as e:
            print(f"[DB Engine] Update check failed: {e}")
            # Fall back to syncing if we have nothing locally.
            return "force-empty-db" if self._entry_count() == 0 else None

        if current_etag and current_etag == new_etag and self._entry_count() > 0:
            return None

        return new_etag or "force-empty-db"

    # ------------------------------------------------------------------ #
    # AniList metadata
    # ------------------------------------------------------------------ #
    async def _fetch_anilist_metadata_bulk(self, anilist_ids: List[int]) -> Dict[int, Dict]:
        """
        Fetch titles/format for many AniList ids using aliased GraphQL queries.

        Non-fatal: a failing chunk is logged and skipped so the mapping can still
        be built (titles are best-effort; scrapers fetch titles live at watch time).
        """
        results: Dict[int, Dict] = {}
        chunk_size = self.ANILIST_CHUNK_SIZE

        async with httpx.AsyncClient(timeout=30.0) as client:
            i = 0
            while i < len(anilist_ids):
                chunk = anilist_ids[i:i + chunk_size]
                query_parts = [
                    f"a{idx}: Media(id: {aid}, type: ANIME) {{ "
                    f"id idMal format title {{ romaji english native }} "
                    f"startDate {{ year }} }}"
                    for idx, aid in enumerate(chunk)
                ]
                query = "query { " + " ".join(query_parts) + " }"

                try:
                    response = await client.post(self.ANILIST_API_URL, json={"query": query})
                except Exception as e:
                    print(f"[AniList] Chunk request error (skipping): {e}")
                    i += chunk_size
                    continue

                if response.status_code == 429:
                    retry_after = self._safe_int(response.headers.get("Retry-After")) or 60
                    print(f"[AniList] Rate limited; waiting {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    continue  # retry the same chunk

                if response.status_code != 200:
                    # GraphQL may still return partial data with a non-200; try to use it.
                    print(f"[AniList] Chunk returned status {response.status_code}; attempting partial parse.")

                try:
                    data = response.json().get("data") or {}
                except Exception:
                    data = {}

                for media in data.values():
                    if media and media.get("id"):
                        results[media["id"]] = media

                i += chunk_size
                await asyncio.sleep(self.ANILIST_CHUNK_DELAY)

        return results

    # ------------------------------------------------------------------ #
    # Overrides
    # ------------------------------------------------------------------ #
    def _load_overrides(self) -> Dict[int, Dict[int, int]]:
        """Load overrides.json -> {tmdb_id: {season_number: anilist_id}}."""
        if not self.OVERRIDES_PATH.exists():
            return {}
        try:
            raw = json.loads(self.OVERRIDES_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[DB Engine] Could not read overrides.json: {e}")
            return {}

        parsed: Dict[int, Dict[int, int]] = {}
        for tmdb_key, seasons in (raw.get("seasons") or {}).items():
            tmdb_id = self._safe_int(tmdb_key)
            if tmdb_id is None or not isinstance(seasons, dict):
                continue
            season_map: Dict[int, int] = {}
            for season_key, anilist_value in seasons.items():
                season_num = self._safe_int(season_key)
                anilist_id = self._safe_int(anilist_value)
                if season_num is not None and anilist_id is not None:
                    season_map[season_num] = anilist_id
            if season_map:
                parsed[tmdb_id] = season_map
        return parsed

    # ------------------------------------------------------------------ #
    # Sync
    # ------------------------------------------------------------------ #
    async def sync_database_async(self):
        """Download the Fribb dataset and rebuild the mapping tables."""
        self.init_db()

        async with httpx.AsyncClient(timeout=60.0) as client:
            print("\n--- Mapping sync starting ---")
            new_etag = await self._check_needs_update(client)
            if not new_etag:
                print("[DB Engine] Mappings already up-to-date.")
                return

            print("[DB Engine] Downloading Fribb anime-list...")
            try:
                response = await client.get(self.MAPPING_URL, follow_redirects=True)
                response.raise_for_status()
                anime_data: List[Dict[str, Any]] = response.json()
            except Exception as e:
                print(f"[DB Engine] Download failed: {e}")
                return

        # 1. Group Fribb entries by TMDB tv id.
        groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        for item in anime_data:
            anilist_id = self._safe_int(item.get("anilist_id"))
            tmdb_id = self._tmdb_tv_id(item)
            if not anilist_id or not tmdb_id:
                continue
            groups[tmdb_id].append(
                {
                    "anilist_id": anilist_id,
                    "season_number": self._tmdb_season(item),
                    "mal_id": self._safe_int(item.get("mal_id")),
                    "type": (item.get("type") or "TV").upper(),
                }
            )

        # 2. Resolve season slots vs. extras per show.
        season_rows: List[tuple] = []   # (tmdb_id, season_number, anilist_id)
        extra_rows: List[tuple] = []    # (tmdb_id, anilist_id, anime_type)
        all_anilist_ids: set = set()
        entry_type: Dict[int, str] = {}  # anilist_id -> Fribb type (fallback)

        def _better(candidate: Dict, current: Optional[Dict]) -> bool:
            """Prefer a real TV entry, then the lowest AniList id."""
            if current is None:
                return True
            cand_tv = candidate["type"] == "TV"
            cur_tv = current["type"] == "TV"
            if cand_tv != cur_tv:
                return cand_tv
            return candidate["anilist_id"] < current["anilist_id"]

        for tmdb_id, items in groups.items():
            chosen: Dict[int, Dict] = {}  # season_number -> entry
            leftovers: List[Dict] = []

            for entry in items:
                all_anilist_ids.add(entry["anilist_id"])
                entry_type[entry["anilist_id"]] = entry["type"]
                snum = entry["season_number"]
                if snum is not None and snum >= 1:
                    if _better(entry, chosen.get(snum)):
                        if snum in chosen:
                            leftovers.append(chosen[snum])
                        chosen[snum] = entry
                    else:
                        leftovers.append(entry)
                else:
                    leftovers.append(entry)

            # Fallback: a show with no season>=1 slot but a TV entry -> make it season 1.
            if not chosen:
                tv_entries = [e for e in leftovers if e["type"] == "TV"]
                if tv_entries:
                    best = min(tv_entries, key=lambda e: e["anilist_id"])
                    chosen[1] = best
                    leftovers.remove(best)

            for snum, entry in chosen.items():
                season_rows.append((tmdb_id, snum, entry["anilist_id"]))
            for entry in leftovers:
                extra_rows.append((tmdb_id, entry["anilist_id"], entry["type"]))

        if not season_rows and not extra_rows:
            print("[DB Engine] No mappings parsed from dataset; aborting (DB left intact).")
            return

        # 3. Enrich with AniList titles (best-effort).
        print(f"[DB Engine] Fetching AniList metadata for {len(all_anilist_ids)} ids...")
        al_metadata = await self._fetch_anilist_metadata_bulk(sorted(all_anilist_ids))

        # 4. Build anime_entries rows.
        now = self._now()
        entry_rows: List[tuple] = []
        for aid in all_anilist_ids:
            meta = al_metadata.get(aid, {})
            title = meta.get("title") or {}
            entry_rows.append(
                (
                    aid,
                    meta.get("idMal"),
                    title.get("romaji"),
                    title.get("english"),
                    title.get("native"),
                    meta.get("format") or entry_type.get(aid),
                    (meta.get("startDate") or {}).get("year"),
                    now,
                )
            )

        # 5. Apply overrides (these win the season slot).
        overrides = self._load_overrides()
        if overrides:
            season_map = {(t, s): a for (t, s, a) in season_rows}
            for tmdb_id, seasons in overrides.items():
                for season_num, anilist_id in seasons.items():
                    season_map[(tmdb_id, season_num)] = anilist_id
            season_rows = [(t, s, a) for (t, s), a in season_map.items()]
            print(f"[DB Engine] Applied overrides for {len(overrides)} show(s).")

        # 6. Commit atomically; never wipe to nothing.
        conn = self._connect()
        committed = False
        try:
            cursor = conn.cursor()
            cursor.execute("BEGIN")
            cursor.execute("DELETE FROM anime_entries")
            cursor.execute("DELETE FROM tmdb_seasons")
            cursor.execute("DELETE FROM tmdb_extras")

            cursor.executemany(
                """
                INSERT OR REPLACE INTO anime_entries
                    (anilist_id, mal_id, title_romaji, title_english, title_native,
                     anime_type, start_year, last_synced)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                entry_rows,
            )
            cursor.executemany(
                "INSERT OR REPLACE INTO tmdb_seasons (tmdb_id, season_number, anilist_id) VALUES (?, ?, ?)",
                season_rows,
            )
            cursor.executemany(
                "INSERT OR REPLACE INTO tmdb_extras (tmdb_id, anilist_id, anime_type) VALUES (?, ?, ?)",
                extra_rows,
            )
            cursor.execute(
                "INSERT OR REPLACE INTO sync_meta (key, value) VALUES ('etag', ?)", (new_etag,)
            )
            conn.commit()
            committed = True
        except Exception as e:
            conn.rollback()
            print(f"[DB Engine] Sync failed, rolled back: {e}")
        finally:
            conn.close()

        if committed:
            # Keep this print ASCII-only: some consoles (Windows cp1252) raise on emoji.
            print(
                f"[DB Engine] Sync complete. "
                f"entries={len(entry_rows)} seasons={len(season_rows)} extras={len(extra_rows)}"
            )


async def _main():
    engine = MappingDatabaseEngine()
    await engine.sync_database_async()


if __name__ == "__main__":
    asyncio.run(_main())
