# metadata_engine

TMDB and AniList metadata, and the mapping between them. TMDB models an anime as
one show with numbered seasons; AniList gives every cour, OVA and film its own
id. The [Fribb anime-lists](https://github.com/Fribb/anime-lists) dataset ties
each AniList entry to its TMDB show and season, which is the backbone of the
mapping.

## Modules

| Module | What it does | Tables it writes |
|---|---|---|
| `mapping_sync.py` | Rebuilds the mapping from Fribb plus AniList titles, in one transaction. `init_db` creates every table in this list (startup runs it first). | `anime_entries`, `tmdb_seasons`, `tmdb_extras`, `sync_meta` |
| `fribb.py` | Pure: turns the Fribb dataset into season and extra rows, recovers side content through tvdb ids and AniList relations, reads `overrides.json`. | none |
| `resync.py` | CLI for a forced rebuild (below). | via `mapping_sync` |
| `forced_resync.py` | The admin-triggered rebuild, in-process, with state the dashboard polls. | via `mapping_sync` |
| `sync_status.py` | The startup sync's phase, for `/health`. | none |
| `tmdb.py` | All TMDB HTTP: shows, movies, seasons, search, trending, genre maps, localized titles, IMDb ids. | via `store` |
| `store.py` | Upserts of TMDB show and movie rows, and the tmdb to AniList lookup the search needs. | `tmdb_shows`, `tmdb_movies` |
| `maintenance.py` | Nightly 1/N staleness refresh and the discover backfill, plus the backfill job queue. | `metadata_backfill_jobs` |
| `anilist.py` | All AniList GraphQL: title metadata, browse hubs, manga search, trending and overview. | none |
| `catalogue.py` | Reads over the mapping and catalogue tables. | none |
| `overview.py` | Show, season and title overviews, rebuilt from local rows when TMDB fails. | none |
| `browse.py` | The browse hubs' catalogues from the local tables, cached. | none |
| `search.py` | Anime search, local catalogue first, TMDB when it has too few hits. | none |
| `next_episode.py` | Next-episode hints for Continue Watching. | none |
| `dates.py` | TMDB date string helpers. | none |
| `routes.py`, `discovery_routes.py`, `admin_routes.py` | The HTTP routes: title pages, search/trending/catalogue, and the admin resync and backfill. | none |

## The mapping

| Table | Holds |
|---|---|
| `anime_entries` | AniList titles, `mal_id`, format, start year, genres, and the TMDB movie id of a film. |
| `tmdb_seasons` | One AniList id per real TMDB season, `(tmdb_id, season_number)`, season >= 1. |
| `tmdb_extras` | Specials, OVAs, films and season-collision losers tied to a show. |
| `sync_meta` | The Fribb ETag of the last rebuild. |

A rebuild runs when the Fribb ETag moves or `anime_entries` is empty, on startup
and every 24h on the `RUN_DB_SYNC` replica. A season-slot collision goes to a
real TV entry first, then the lowest AniList id; the loser becomes an extra.
AniList titles are best-effort: a failed chunk does not abort the sync. A
dataset that parses to nothing aborts without touching the tables.

## Fixing a wrong season: `overrides.json`

```json
{ "seasons": { "20111": { "1": 93, "2": 94 } } }
```

Keys are TMDB tv ids, each mapping `season_number` to `anilist_id`. Overrides
are applied last and always win; an AniList id missing from the dataset is
skipped. They take effect on the next rebuild.

## Forcing a rebuild

```bash
docker exec "$(docker ps -q -f name=crimson-api_api-sync)" python -m metadata_engine.resync
```

Or the admin dashboard's resync button. Either way the live replicas keep
serving the previous snapshot until the rebuild commits.
