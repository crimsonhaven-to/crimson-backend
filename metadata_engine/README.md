# Metadata Engine

Builds the PostgreSQL mapping between **TMDB tv ids** (the public key used by
the API and frontend) and **AniList ids** (one per cour/season/OVA/movie). The
tables are stored in the shared database and accessed through `db_pool`.

## Why this exists

TMDB models an anime as a single show with numbered seasons. AniList gives every
release its own id. To stream "Show X, Season 2, Episode 3" we must resolve the
correct AniList id for that season. The [Fribb anime-lists](https://github.com/Fribb/anime-lists)
dataset provides, per AniList entry, the parent `themoviedb_id.tv` and the
`season.tmdb` it belongs to — that is the backbone of the mapping.

## Data source

`anime-list-full.json` from Fribb, fetched on demand. A GitHub `ETag` is stored
in `sync_meta` so we only rebuild when upstream changes (a rebuild is also forced
whenever `anime_entries` is empty, so a wiped DB self-heals).

## Schema

| Table          | Purpose |
|----------------|---------|
| `anime_entries`| AniList metadata: titles (romaji/english/native), `mal_id`, `anime_type` (`format`: TV/MOVIE/OVA/…), `start_year`. |
| `tmdb_seasons` | One AniList id per real TMDB season. PK `(tmdb_id, season_number)`, `season_number >= 1`. |
| `tmdb_extras`  | Specials / OVAs / movies (and season-collision losers) tied to a show. PK `(tmdb_id, anilist_id)`. |
| `tmdb_shows`   | TMDB show details, populated **lazily** by the API on first request. |
| `sync_meta`    | Sync bookkeeping (ETag). |
| `api_cache`    | Generic response cache used by `api.py`. |

## Sync flow (`sync_database_async`)

1. ETag check (skip if unchanged and DB non-empty).
2. Download Fribb JSON, group entries by TMDB tv id.
3. Per show: entries with `season.tmdb >= 1` claim a season slot; everything else
   becomes an extra. A show with no numbered season but a TV entry falls back to
   season 1.
4. Season-slot collisions resolve deterministically: prefer a real `TV` entry,
   then the lowest AniList id. The loser is kept as an extra (nothing is lost).
5. Bulk-fetch AniList titles (`idMal`, `format`, `title{...}`, `startDate.year`)
   in aliased chunks, honoring rate limits. Best-effort — a failed chunk does not
   abort the sync.
6. Apply `overrides.json` (always wins).
7. Rebuild the tables in one transaction; abort if nothing was parsed (never wipe
   the DB to empty).

## Maintenance: `overrides.json`

When the automatic mapping is wrong for a specific show, add one line:

```json
{
  "seasons": {
    "20111": { "1": 93, "2": 94 }
  }
}
```

Keys are TMDB tv ids; each maps `season_number` → `anilist_id`. Overrides are
applied last and always win. Re-run the sync to apply.

## Running standalone

```bash
python -m metadata_engine.db_handler   # requires TMDB_API_KEY in env (optional for the mapping)
```

The API also runs this on startup and on a 24h schedule (see `api.py`).
