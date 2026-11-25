# Music genres and search

Status: implemented (backend 18.15.0, client 13.14.0). Member-facing behaviour is
documented in crimson-docs under *Self-hosting › Music › Genres and search*. This
page records how it is built and why it differs from the first proposal.

## Data

| Where | Holds | Written by |
| --- | --- | --- |
| `music_tracks.spotify_artist_ids` | Spotify ids of the track's artists | `save_tracks` (import, sync), `genres_db.set_artist_ids` (name lookup) |
| `music_tracks.genres` | genres an import carried for this track alone | `save_tracks` (Exportify `Genres` column) |
| `music_tracks.artists_checked_at` | the name lookup ran, found or not | `genres_db.set_artist_ids` |
| `music_artists` | one row per Spotify artist: name, genres, fetched_at | `genres_db.save_artist` |

A track's genres are the union of its own and its artists', computed when the
library is read (`genres_db.TRACK_GENRES_T`). Nothing is copied per track, so a
genre that arrives later shows up everywhere without a backfill.

On a re-import, artist ids and genres from the import replace stored ones unless
the import has none. Everything else on a track keeps first-import-wins.

## Enrichment

`genres.enrich` runs on every music-worker poll tick with a budget of
`GENRE_BATCH` Spotify requests:

1. Artist ids no `music_artists` row covers: `GET /artists/{id}` each. A 400 or
   404 stores an empty row so it is not asked again. Any other failure pauses
   genre lookups for `BACKOFF_SECONDS`; downloads carry on.
2. Ready tracks with no artist ids and no `artists_checked_at`: Spotify search
   for the first artist, accepted only on an exact, case-insensitive name match.
   The artist's genres come from step 1 on a later tick.

Requests go through the most recently updated `music_spotify_links` row. Artists
are the same for every member, so whose link it is does not matter.

Migration 015 clears `snapshot_id` and `last_synced_at` on Spotify playlists, so
the next sync check reads them in full and fills artist ids for older tracks.

## Why not the first proposal

| Proposed | Built | Reason |
| --- | --- | --- |
| `GET /v1/artists?ids=` in batches of 50 | `GET /artists/{id}` one by one, cached | The batch endpoint was removed for development-mode apps in February 2026. `genres` on the artist object was kept. |
| `music_track_genres` and `music_track_artists` tables | two JSONB columns and `music_artists` | No second copy of `artists` to keep in sync, and genres stay current without a write per track. |
| `sort`, `genre`, `artist`, `q` on `/music/all-titles`, plus `/music/genres` and `/music/artists` | `genres` on every track payload, filtering in the client | Both views already load every track, and offline playback has no server to ask. |
| `ffprobe` of existing files for a genre tag | not built | YouTube sourced files rarely carry one. The Spotify name lookup covers those songs better. |

Existing `.m4a` files are not re-tagged; new downloads get the first three
genres in their `genre` tag.
