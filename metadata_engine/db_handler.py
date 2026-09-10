"""
Metadata mapping engine.

Builds the PostgreSQL mapping between TMDB tv ids and AniList ids from the Fribb
anime-lists dataset, enriched with AniList titles.

Design
------
TMDB groups an anime as one show with numbered seasons, while AniList gives every
cour, OVA and movie its own id. Fribb provides, per AniList entry, the parent
``themoviedb_id.tv`` plus the TMDB season it maps to. That field is trusted for
real TV seasons and everything else is split off:

* ``tmdb_seasons``  one AniList id per (tmdb_id, season_number), season >= 1
* ``tmdb_extras``   every other entry tied to the show, plus the losers of a
                    season collision, so nothing is lost

Collisions on a season slot resolve deterministically: a real TV entry first,
then the lowest AniList id. ``overrides.json`` is applied last and always wins,
the single maintenance lever for the long tail.

Reaching the specials and movies Fribb hides
--------------------------------------------
``themoviedb_id.tv`` alone loses most of a franchise's side content, two ways:

1. A film TMDB tracks as a standalone movie carries a movie id and no ``tv`` key,
   so it has no show to group under. When such an entry repeats its parent's
   ``tvdb_id``, it is attached through a tvdb_id -> tmdb_id map built from the
   entries carrying both, keeping its own movie id so it plays through the movie
   route. A film with no parent still gets an ``anime_entries`` row, so the
   catalogue can list it.
2. Roughly a third of the dataset carries no external id at all, so Fribb offers
   no key to tie those to a show. AniList does: the ``relations`` edges of the ids
   already mapped name them directly. So the bulk fetch also pulls relations, and
   side-content edges off any mapped entry become extras of that show.
"""

import asyncio
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from core.db_pool import get_connection, lock_schema_init

_THIS_DIR = Path(__file__).resolve().parent

# Which relation edges count as side content. SIDE_STORY, SUMMARY and SPECIAL are
# the specials, recap films and shorts; PARENT catches an entry pointing up at the
# main series; ALTERNATIVE covers re-cuts. Excluded on purpose: SEQUEL and PREQUEL
# (a season or a show in its own right), CHARACTER (crossovers), ADAPTATION and
# SOURCE (the novel), and OTHER (too noisy).
_EXTRA_RELATIONS = {"SIDE_STORY", "SUMMARY", "SPECIAL", "PARENT", "ALTERNATIVE"}

# ...and only when the related entry is itself side content, which keeps a
# SIDE_STORY edge pointing at a full TV series out of the extras.
_EXTRA_FORMATS = {"SPECIAL", "OVA", "ONA", "MOVIE"}


class MappingDatabaseEngine:
    MAPPING_URL = "https://raw.githubusercontent.com/Fribb/anime-lists/master/anime-list-full.json"
    ANILIST_API_URL = "https://graphql.anilist.co"
    OVERRIDES_PATH = _THIS_DIR / "overrides.json"

    # AniList caps query complexity at 500, and the relations block (the only
    # route to the specials Fribb has no id for) costs ~22 per aliased Media. So
    # 25 aliases is rejected at 550 while 20 lands at ~440. Raising this without
    # re-measuring will 400 the whole chunk.
    ANILIST_CHUNK_SIZE = 20
    ANILIST_CHUNK_DELAY = 0.7  # rate-limit friendly

    def __init__(self, db_name: str = "anime_mappings.db", tmdb_api_key: Optional[str] = None):
        # Ignored; kept for call-site compatibility. Storage is the shared pool.
        self.db_name = db_name
        self.tmdb_api_key = tmdb_api_key or os.getenv("TMDB_API_KEY")

    # --- helpers ------------------------------------------------------- #
    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        """Best-effort int, else None."""
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
        """The TMDB tv id from a Fribb entry, in dict or scalar form."""
        raw = item.get("themoviedb_id")
        if isinstance(raw, dict):
            return MappingDatabaseEngine._safe_int(raw.get("tv"))
        return MappingDatabaseEngine._safe_int(raw)

    @staticmethod
    def _tmdb_movie_id(item: Dict[str, Any]) -> Optional[int]:
        """The TMDB movie id from a Fribb entry, if it has one.

        A film TMDB tracks in its own right carries a movie id list, one per part
        for split releases, instead of a ``tv`` key. Kept alongside the parent show
        so it plays through the movie route rather than being squeezed into a
        season/episode URL that does not exist.
        """
        raw = item.get("themoviedb_id")
        if not isinstance(raw, dict):
            return None
        movie = raw.get("movie")
        if isinstance(movie, list):
            movie = movie[0] if movie else None
        return MappingDatabaseEngine._safe_int(movie)

    @staticmethod
    def _tmdb_season(item: Dict[str, Any]) -> Optional[int]:
        """The TMDB season number Fribb assigns to this entry."""
        season = item.get("season")
        if isinstance(season, dict):
            return MappingDatabaseEngine._safe_int(season.get("tmdb"))
        return MappingDatabaseEngine._safe_int(season)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _connect(self):
        # Commits on a clean exit, rolls back on exception. MVCC means readers see
        # the pre-DELETE snapshot until a rebuild commits, so a wholesale resync
        # never contends with them.
        return get_connection()

    # --- schema -------------------------------------------------------- #
    def init_db(self):
        """Create the schema (idempotent) and drop obsolete tables."""
        with self._connect() as conn:
            cursor = conn.cursor()

            # Serialize DDL across replicas.
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

            # CREATE TABLE IF NOT EXISTS won't add a column to an existing table.
            # Stays null until the next sync rebuild repopulates the table.
            cursor.execute("ALTER TABLE anime_entries ADD COLUMN IF NOT EXISTS genres TEXT")

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

            # A JSON list of genre names, lazily populated by fetch_tmdb_show.
            cursor.execute("ALTER TABLE tmdb_shows ADD COLUMN IF NOT EXISTS genres TEXT")
            # The /catalogue/shows browse orders by this. Null on pre-existing rows
            # until a backfill or refresh repopulates them.
            cursor.execute("ALTER TABLE tmdb_shows ADD COLUMN IF NOT EXISTS popularity DOUBLE PRECISION")
            # Drives the staleness refresher. NULL on pre-existing rows, so they
            # sort oldest-first and are refreshed before anything else.
            cursor.execute("ALTER TABLE tmdb_shows ADD COLUMN IF NOT EXISTS last_updated TIMESTAMP")
            # Keeps the refresher's ORDER BY cheap.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_shows_last_updated ON tmdb_shows(last_updated)")

            # Separate from tmdb_shows because the tv and movie id spaces overlap
            # numerically. Lazily populated like tmdb_shows, and never touched by
            # the Fribb resync.
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

            # As for tmdb_shows above.
            cursor.execute("ALTER TABLE tmdb_movies ADD COLUMN IF NOT EXISTS genres TEXT")
            # The /catalogue/movies default order. Distinct from vote_average.
            cursor.execute("ALTER TABLE tmdb_movies ADD COLUMN IF NOT EXISTS popularity DOUBLE PRECISION")
            # Persisted rather than left in the api_cache JSON, so the table can be
            # sorted by them and they survive a cache miss.
            cursor.execute("ALTER TABLE tmdb_movies ADD COLUMN IF NOT EXISTS runtime INTEGER")
            cursor.execute("ALTER TABLE tmdb_movies ADD COLUMN IF NOT EXISTS vote_average DOUBLE PRECISION")
            cursor.execute("ALTER TABLE tmdb_movies ADD COLUMN IF NOT EXISTS status TEXT")
            cursor.execute("ALTER TABLE tmdb_movies ADD COLUMN IF NOT EXISTS original_title TEXT")
            # Staleness bookkeeping, mirroring tmdb_shows above.
            cursor.execute("ALTER TABLE tmdb_movies ADD COLUMN IF NOT EXISTS last_updated TIMESTAMP")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_movies_last_updated ON tmdb_movies(last_updated)")

            # The admin button runs on a serving replica that cannot reach the
            # portless api-sync container, so the request is written here and the
            # sync replica claims it. The same DB-as-queue pattern cache_engine
            # uses. Status walks requested -> running -> done|failed.
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
            # Indexes the drainer's probe for the oldest 'requested' row.
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_backfill_jobs_status_req "
                "ON metadata_backfill_jobs(status, requested_at)"
            )

            # One AniList id per real TMDB season.
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

            # Specials, OVAs, movies and season-collision losers tied to a show.
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

            # For the reverse anilist -> tmdb lookups.
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_seasons_anilist ON tmdb_seasons(anilist_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_extras_anilist ON tmdb_extras(anilist_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tmdb_extras_show ON tmdb_extras(tmdb_id)")

            # From earlier schema iterations, if a persisted DB still has them.
            for legacy in ("show_groups", "group_members", "mappings", "season_groups"):
                cursor.execute(f"DROP TABLE IF EXISTS {legacy}")

        print("[DB Engine] Schema ready (PostgreSQL).")

    def _entry_count(self) -> int:
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) AS n FROM anime_entries")
                return cursor.fetchone()["n"]
        except Exception:
            return 0

    # --- update detection ---------------------------------------------- #
    async def _check_needs_update(self, client: httpx.AsyncClient) -> Optional[str]:
        """The ETag to sync against, or None when already up to date.

        An empty local DB always resyncs, which self-heals a wiped DB even when
        the upstream ETag has not moved.
        """
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM sync_meta WHERE key = 'etag'")
            row = cursor.fetchone()
            current_etag = row["value"] if row else None

        try:
            response = await client.head(self.MAPPING_URL, follow_redirects=True)
            new_etag = response.headers.get("ETag")
        except Exception as e:
            print(f"[DB Engine] Update check failed: {e}")
            # Sync anyway if there is nothing locally.
            return "force-empty-db" if self._entry_count() == 0 else None

        if current_etag and current_etag == new_etag and self._entry_count() > 0:
            return None

        return new_etag or "force-empty-db"

    # --- AniList metadata ---------------------------------------------- #
    async def _fetch_anilist_metadata_bulk(self, anilist_ids: List[int]) -> Dict[int, Dict]:
        """Titles, format and relations for many AniList ids, via aliased queries.

        The ``relations`` block recovers the side content Fribb carries no id for.
        Each edge's node brings its own format, title and year, so an extra found
        this way needs no second lookup.

        Non-fatal: a failing chunk is logged and skipped, since titles are
        best-effort and scrapers fetch them live at watch time.
        """
        results: Dict[int, Dict] = {}
        chunk_size = self.ANILIST_CHUNK_SIZE

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
                    print(f"[AniList] Chunk request error (skipping): {e}")
                    i += chunk_size
                    continue

                if response.status_code == 429:
                    retry_after = self._safe_int(response.headers.get("Retry-After")) or 60
                    print(f"[AniList] Rate limited; waiting {retry_after}s...")
                    await asyncio.sleep(retry_after)
                    continue  # retry the same chunk

                try:
                    body = response.json()
                except Exception:
                    body = {}

                if response.status_code != 200:
                    # A non-200 may still carry partial data, used below. Surface
                    # AniList's own error messages though: a swallowed complexity
                    # 400 otherwise drops the whole chunk's titles with no clue why.
                    errs = body.get("errors") if isinstance(body, dict) else None
                    detail = "; ".join(
                        str(e.get("message", e)) for e in errs[:3]
                    ) if errs else "no error detail"
                    print(f"[AniList] Chunk returned status {response.status_code}: {detail}")

                data = (body.get("data") if isinstance(body, dict) else None) or {}

                for media in data.values():
                    if media and media.get("id"):
                        results[media["id"]] = media

                i += chunk_size
                await asyncio.sleep(self.ANILIST_CHUNK_DELAY)

        return results

    # --- grouping ------------------------------------------------------ #
    @classmethod
    def _group_by_show(cls, anime_data: List[Dict[str, Any]]):
        """Bucket the Fribb dataset by the TMDB show each entry belongs to.

        Returns ``(groups, movie_id_by_anilist, orphan_movies)``, where the second
        maps every film that has a movie id, parent or not, and the third holds the
        films with no parent, which still deserve a catalogue row.

        The key is ``themoviedb_id.tv`` where present. Where it is not, a film
        repeating its parent series' ``tvdb_id`` attaches to whatever show that
        resolves to. Such an entry is side content by definition, since a real
        season always carries its own ``themoviedb_id.tv``, so its season is forced
        to None and it lands in tmdb_extras.
        """
        # Learned from the entries carrying both. First writer wins, since a tvdb
        # series maps to exactly one TMDB show.
        tvdb_to_tmdb: Dict[int, int] = {}
        for item in anime_data:
            tvdb_id = cls._safe_int(item.get("tvdb_id"))
            tmdb_id = cls._tmdb_tv_id(item)
            if tvdb_id and tmdb_id:
                tvdb_to_tmdb.setdefault(tvdb_id, tmdb_id)

        groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
        movie_id_by_anilist: Dict[int, int] = {}
        orphan_movies: set = set()

        for item in anime_data:
            anilist_id = cls._safe_int(item.get("anilist_id"))
            if not anilist_id:
                continue
            movie_id = cls._tmdb_movie_id(item)
            if movie_id:
                movie_id_by_anilist.setdefault(anilist_id, movie_id)

            tmdb_id = cls._tmdb_tv_id(item)
            attached_by_tvdb = False
            if not tmdb_id:
                tmdb_id = tvdb_to_tmdb.get(cls._safe_int(item.get("tvdb_id")))
                attached_by_tvdb = tmdb_id is not None

            if not tmdb_id:
                # Nothing to hang it off. A standalone film is still listable
                # through its own movie id; anything else waits for the relations
                # pass.
                if movie_id:
                    orphan_movies.add(anilist_id)
                continue

            groups[tmdb_id].append(
                {
                    "anilist_id": anilist_id,
                    "season_number": None if attached_by_tvdb else cls._tmdb_season(item),
                    "mal_id": cls._safe_int(item.get("mal_id")),
                    "type": (item.get("type") or "TV").upper(),
                }
            )

        return groups, movie_id_by_anilist, orphan_movies

    # --- relation-derived extras --------------------------------------- #
    @staticmethod
    def _relation_extras(season_rows: List[tuple], extra_rows: List[tuple],
                         al_metadata: Dict[int, Dict]) -> List[tuple]:
        """Find each show's side content among the AniList relations of the entries
        already mapped to it.

        Roughly a third of the Fribb dataset carries no external id, so those
        entries can never be grouped by id. AniList names them: a franchise's recap
        films and specials are SUMMARY or SIDE_STORY edges of ids that *are* mapped.

        Walks one hop out from every mapped member of a show, which is what picks
        up a film hanging off a later season rather than season 1. It deliberately
        does not walk the discovered entries in turn: their own relations were
        never fetched, and following them would drift into neighbouring franchises.

        Returns tuples for the new extras only, carrying the edge node so it can be
        reused as that entry's metadata with no second fetch.
        """
        members: Dict[int, List[int]] = defaultdict(list)
        for tmdb_id, _season, anilist_id in season_rows:
            members[tmdb_id].append(anilist_id)
        for tmdb_id, anilist_id, *_rest in extra_rows:
            members[tmdb_id].append(anilist_id)

        # An id that already owns a season slot is a series in its own right, so
        # never re-file it as somebody's special.
        season_ids = {anilist_id for _t, _s, anilist_id in season_rows}
        claimed = {(tmdb_id, anilist_id) for tmdb_id, anilist_id, *_r in extra_rows}

        found: List[tuple] = []
        for tmdb_id, member_ids in members.items():
            for member_id in member_ids:
                relations = (al_metadata.get(member_id) or {}).get("relations") or {}
                for edge in relations.get("edges") or []:
                    if (edge or {}).get("relationType") not in _EXTRA_RELATIONS:
                        continue
                    node = edge.get("node") or {}
                    node_id = MappingDatabaseEngine._safe_int(node.get("id"))
                    node_format = node.get("format")
                    if not node_id or node_format not in _EXTRA_FORMATS:
                        continue
                    if node_id in season_ids or (tmdb_id, node_id) in claimed:
                        continue
                    claimed.add((tmdb_id, node_id))
                    found.append((tmdb_id, node_id, node_format, node))
        return found

    # --- overrides ----------------------------------------------------- #
    def _load_overrides(self) -> Dict[int, Dict[int, int]]:
        """Load overrides.json into {tmdb_id: {season_number: anilist_id}}."""
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

    # --- sync ---------------------------------------------------------- #
    async def sync_database_async(self, force: bool = False) -> str:
        """Download the Fribb dataset and rebuild the mapping tables.

        ``force`` rebuilds even when the upstream ETag is unchanged, which is how
        the manual resync backfills after a schema change without waiting for
        Fribb to move.

        Returns an outcome string so callers need not re-derive what happened:
        ``"up_to_date"``, ``"synced"``, ``"empty"`` (upstream parsed to no
        mappings, DB intact) or ``"failed"``.
        """
        self.init_db()

        async with httpx.AsyncClient(timeout=60.0) as client:
            print("\n--- Mapping sync starting ---")
            new_etag = await self._check_needs_update(client)
            if not new_etag:
                if not force:
                    print("[DB Engine] Mappings already up-to-date.")
                    return "up_to_date"
                # Capture the current ETag so sync_meta stays in step and the next
                # scheduled check doesn't see a phantom change.
                try:
                    head = await client.head(self.MAPPING_URL, follow_redirects=True)
                    new_etag = head.headers.get("ETag") or "forced-resync"
                except Exception:
                    new_etag = "forced-resync"
                print("[DB Engine] Forced resync: rebuilding despite up-to-date ETag.")

            print("[DB Engine] Downloading Fribb anime-list...")
            try:
                response = await client.get(self.MAPPING_URL, follow_redirects=True)
                response.raise_for_status()
                anime_data: List[Dict[str, Any]] = response.json()
            except Exception as e:
                print(f"[DB Engine] Download failed: {e}")
                return "failed"

        # 1. Group Fribb entries by the TMDB show they belong to.
        groups, movie_id_by_anilist, orphan_movies = self._group_by_show(anime_data)

        # 2. Resolve season slots against extras, per show.
        season_rows: List[tuple] = []   # (tmdb_id, season_number, anilist_id)
        extra_rows: List[tuple] = []    # (tmdb_id, anilist_id, anime_type, tmdb_movie_id)
        all_anilist_ids: set = set(orphan_movies)
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

            # A show with no season slot but a TV entry becomes season 1.
            if not chosen:
                tv_entries = [e for e in leftovers if e["type"] == "TV"]
                if tv_entries:
                    best = min(tv_entries, key=lambda e: e["anilist_id"])
                    chosen[1] = best
                    leftovers.remove(best)

            for snum, entry in chosen.items():
                season_rows.append((tmdb_id, snum, entry["anilist_id"]))
            for entry in leftovers:
                extra_rows.append((tmdb_id, entry["anilist_id"], entry["type"],
                                   movie_id_by_anilist.get(entry["anilist_id"])))

        if not season_rows and not extra_rows:
            print("[DB Engine] No mappings parsed from dataset; aborting (DB left intact).")
            return "empty"

        # 3. Enrich with AniList titles, best-effort.
        print(f"[DB Engine] Fetching AniList metadata for {len(all_anilist_ids)} ids...")
        al_metadata = await self._fetch_anilist_metadata_bulk(sorted(all_anilist_ids))

        # 3b. Extras Fribb has no id for, named by AniList relations instead.
        relation_rows = self._relation_extras(season_rows, extra_rows, al_metadata)
        for tmdb_id, anilist_id, _fmt, node in relation_rows:
            extra_rows.append((tmdb_id, anilist_id, _fmt, movie_id_by_anilist.get(anilist_id)))
            # The node already carries format, title and year.
            if anilist_id not in all_anilist_ids:
                all_anilist_ids.add(anilist_id)
                al_metadata.setdefault(anilist_id, node)
        if relation_rows:
            print(f"[DB Engine] AniList relations added {len(relation_rows)} extra(s).")

        # 4. Build anime_entries rows.
        now = self._now()
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

        # 5. Apply overrides (these win the season slot).
        overrides = self._load_overrides()
        if overrides:
            season_map = {(t, s): a for (t, s, a) in season_rows}
            for tmdb_id, seasons in overrides.items():
                for season_num, anilist_id in seasons.items():
                    season_map[(tmdb_id, season_num)] = anilist_id
            season_rows = [(t, s, a) for (t, s), a in season_map.items()]
            print(f"[DB Engine] Applied overrides for {len(overrides)} show(s).")

        # 6. One transaction, so a failure never wipes the tables to nothing.
        # Children are deleted before parents to satisfy the foreign keys, then
        # parents are inserted before children for the same reason.
        committed = False
        catalogue_cache_purged = 0
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
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
                    (new_etag,),
                )
                # Without this the rebuild's titles stay masked by the cached
                # /catalogue for up to 6h. In the same transaction, so the cache is
                # dropped if and only if the rebuild commits, and the LIKE matches
                # any future cache-version bump. Only the L2 cache is reachable
                # here; each replica's L1 expires on its own short TTL.
                cursor.execute(
                    "DELETE FROM api_cache WHERE cache_key LIKE %s",
                    ("catalogue:%",),
                )
                catalogue_cache_purged = cursor.rowcount or 0
            committed = True
        except Exception as e:
            print(f"[DB Engine] Sync failed, rolled back: {e}")

        if committed:
            # ASCII only: some consoles raise on emoji.
            print(
                f"[DB Engine] Sync complete. "
                f"entries={len(entry_rows)} seasons={len(season_rows)} extras={len(extra_rows)} "
                f"catalogue_cache_purged={catalogue_cache_purged}"
            )
            return "synced"

        # The transaction rolled back, so MVCC keeps the previous snapshot live.
        return "failed"


async def _main():
    engine = MappingDatabaseEngine()
    await engine.sync_database_async()


if __name__ == "__main__":
    asyncio.run(_main())
