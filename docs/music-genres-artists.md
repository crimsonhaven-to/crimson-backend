# Music Library: Genres, Artist Sorting, and Artist Search

Status: Design proposal (not implemented).

This document proposes how to extend the `All titles` view (`GET /music/all-titles`) with genre and artist sorting, genre filtering, and artist search. It builds directly on the existing `music_engine/` schema (`migrations/008_music.sql` through `012_music_playlist_covers.sql`), worker pipeline, and client conventions without introducing external services or heavyweight search engines.

## 1. Goals and constraints

- Scope every query strictly to the authenticated user (`require_music_user`) and active playlist memberships (`NOT pt.removed_upstream`), matching `GET /music/all-titles`.
- Keep `music_tracks` shared across users and playlists so genre and artist metadata is fetched and stored once per recording.
- Avoid rewriting or re-uploading already-mirrored CDN audio files unless an operator explicitly asks for it.
- Degrade cleanly when a track has no genre (search-added local songs, public-link imports, or niche artists Spotify does not tag) by placing it in an explicit "No genre" bucket rather than hiding it.

## 2. Where genre data comes from

`music_engine/tagging.py` currently writes `title`, `artist`, `album`, `album_artist`, `track`, `disc`, and `date` into each `.m4a`, and does not write a `genre` atom (`©gen`). `music_tracks` has no genre column, and `music_engine/provider.py` (`ImportedTrack` and `Fetched`) does not carry genre fields yet.

Genres can be collected from four sources, in descending order of reliability:

| Priority | Source | Where it runs | Coverage and caveats |
| --- | --- | --- | --- |
| 1 | Spotify artist genres (`GET /v1/artists?ids=...`) | Import, sync, and worker backfill | Spotify track objects do not carry genres, and album genres are mostly empty. Genres live on artist objects. `_TRACK_FIELDS` in `music_engine/spotify.py` currently requests `artists(name)`; changing it to `artists(id,name)` exposes artist IDs at zero extra cost during playlist reads. Requires a connected Spotify link. Spotify returns fine-grained micro-genres (for example `japanese city pop`, `alternative metal`), and newer or niche artists often return `[]`. |
| 2 | Exportify CSV `Artist Genres` column | `music_engine/csv_import.py` | Free during CSV import. Exportify already includes an `Artist Genres` column (comma-separated) that `_COLUMNS` currently ignores. Useful for users who cannot connect a Spotify app. |
| 3 | Provider metadata / source file tags | `music_engine/worker.py` + `core/ffmpeg.py` | When a song is added via search (`tags_from_source = TRUE`) or imported from a public link, there is no Spotify API token. The provider can optionally populate `Fetched.genres`, or the worker can run `ffprobe` on `fetched.path` before `tagging.write_tagged` to read `format.tags.genre`. Coverage depends on the upstream recording and is often empty or generic (such as `Music` or `Pop`). |
| 4 | Fallback ("No genre" bucket) | Query time | Any track with zero genre rows stays valid and appears under an explicit "No genre" filter bucket. Do not insert a fake `"no genre"` string into the database: querying `NOT EXISTS` avoids deleting sentinel rows whenever a real genre arrives later. |

### Normalization and file tagging

- Normalize genre strings on write: trim whitespace, collapse internal spaces, lowercase, split multi-genre tag strings on `,`, `;`, or `/`, drop empty values and known noise strings (like `"other"` or `"unknown"`), and deduplicate per track.
- Keep the normalized genre names as returned rather than forcing a lossy taxonomy upfront. If Spotify's micro-genres feel too fragmented in practice, a static parent-genre mapping table can be layered on in a later phase without re-fetching anything.
- For newly downloaded tracks, update `tagging.tag_args` in `music_engine/tagging.py` to pass `-metadata genre=<joined genres>` (for example `"; ".join(genres[:3])`) when genres are known. That keeps the SMB share self-describing for Jellyfin, Navidrome, and car players, matching the intent documented in `music_engine/fs.py`.

## 3. Schema changes

### Chosen design: normalized satellite tables (`music_track_genres`, `music_track_artists`, `music_artist_cache`)

Two options were considered for storing genres on tracks:

1. **A `genres JSONB NOT NULL DEFAULT '[]'` column on `music_tracks`** with a GIN index.
2. **A normalized `music_track_genres` table** keyed by `(track_id, genre)`.

**Recommendation: `music_track_genres` (option 2).**

Why:

- `GET /music/genres` needs counts per genre scoped to a user's playlists (`GROUP BY genre`). On a normalized table this is a plain join and index scan; on a JSONB array it requires `jsonb_array_elements_text` lateral expansion on every request.
- Filtering by a single genre (`WHERE tg.genre = %s`) and sorting by primary genre use standard B-tree indexes with predictable plans.
- Storing `source` (`'spotify'`, `'csv'`, `'file_tag'`, `'manual'`) per row prevents a weak fallback from overwriting higher-confidence data and supports future manual edits.
- It matches the existing relational style of `music_playlist_tracks` and `music_listens` with `ON DELETE CASCADE`.

### Artist indexing

Today `music_tracks.artists` is `JSONB NOT NULL DEFAULT '[]'`. That works well for rendering a track row (`track_payload`), and `search_library` currently casts `t.artists::text ILIKE %s`, which cannot use a B-tree index and cannot group by individual artist cleanly when a track has multiple artists (for example `["Daft Punk", "Pharrell Williams"]`).

Adding a companion `music_track_artists` table populated whenever `save_tracks` or `mark_ready` writes `artists` solves per-artist grouping, exact artist filtering, and fast trigram artist search without changing `music_tracks.artists` or `track_payload`.

### Proposed migration (`migrations/015_music_genres_artists.sql`, future)

```sql
-- Track when genre enrichment last ran so empty results are not retried forever.
ALTER TABLE music_tracks
    ADD COLUMN IF NOT EXISTS genres_checked_at TEXT;

-- Normalized genres per track.
CREATE TABLE IF NOT EXISTS music_track_genres (
    track_id   BIGINT NOT NULL REFERENCES music_tracks(id) ON DELETE CASCADE,
    genre      TEXT NOT NULL,
    source     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (track_id, genre)
);
CREATE INDEX IF NOT EXISTS idx_music_track_genres_genre
    ON music_track_genres (genre, track_id);

-- Normalized artists per track, kept in sync with music_tracks.artists.
CREATE TABLE IF NOT EXISTS music_track_artists (
    track_id          BIGINT NOT NULL REFERENCES music_tracks(id) ON DELETE CASCADE,
    position          INTEGER NOT NULL,
    artist_name       TEXT NOT NULL,
    artist_name_norm  TEXT NOT NULL,
    spotify_artist_id TEXT,
    PRIMARY KEY (track_id, position)
);
CREATE INDEX IF NOT EXISTS idx_music_track_artists_norm
    ON music_track_artists (artist_name_norm, track_id);
CREATE INDEX IF NOT EXISTS idx_music_track_artists_spotify_id
    ON music_track_artists (spotify_artist_id)
    WHERE spotify_artist_id IS NOT NULL;

-- Cache Spotify /v1/artists responses so shared artists are fetched once across
-- all users and playlists.
CREATE TABLE IF NOT EXISTS music_artist_cache (
    spotify_artist_id TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    genres            JSONB NOT NULL DEFAULT '[]',
    fetched_at        TEXT NOT NULL
);
```

Trigram index for substring artist search, guarded with the exact pattern from `migrations/003_search_trgm.sql` so a restricted DB role never aborts the migration transaction:

```sql
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm') THEN
        CREATE INDEX IF NOT EXISTS idx_music_track_artists_name_trgm
            ON music_track_artists USING GIN (artist_name_norm gin_trgm_ops);
    END IF;
END
$$;
```

## 4. Backfill for existing ready files

Existing libraries already have `status = 'ready'` rows in Postgres, `.m4a` files on the SMB share (`MUSIC_ROOT`), and possibly copies on the CDN (`mirrored_at IS NOT NULL`).

### Step A: Populate `music_track_artists` (instant, local SQL)

A single SQL statement in the migration (or post-migration backfill) populates `music_track_artists` from `music_tracks.artists` using `jsonb_array_elements_text(artists) WITH ORDINALITY`. For 10,000 tracks this takes well under a second and requires zero external calls.

### Step B: Backfill genres in Postgres (worker background pass)

Run a low-priority enrichment pass inside `music_engine/worker.py` (similar to `_mirror_some`, bounded to a small batch per poll tick so it never stalls downloads):

1. **Spotify-backed tracks** (`spotify_id IS NOT NULL AND genres_checked_at IS NULL`):
   - Group up to 50 tracks owned by a user with an active row in `music_spotify_links`.
   - Call `GET /v1/tracks?ids=...` (1 request per 50 tracks) to obtain `artists[].id` when `music_track_artists.spotify_artist_id` is still null.
   - Collect artist IDs missing from (or stale in, for example older than 30 days) `music_artist_cache`.
   - Call `GET /v1/artists?ids=...` in chunks of 50, upsert `music_artist_cache`, insert into `music_track_genres` with `source = 'spotify'`, and stamp `genres_checked_at`.
   - Cost estimate: 1,000 tracks with 300 distinct artists takes 20 track calls plus 6 artist calls (26 requests total), completing in a few seconds while respecting `SpotifyClient`'s existing 429 `Retry-After` backoff.
2. **Non-Spotify tracks or tracks without a connected Spotify link** (`genres_checked_at IS NULL`):
   - If `status = 'ready'` and `rel_path` is present on `MUSIC_ROOT`, run `ffprobe -v quiet -print_format json -show_format` via `core/ffmpeg.py` to inspect `format.tags.genre`.
   - Honest caveat: `tagging.py` currently copies global metadata from input 0 by default, so some files may retain an upstream genre tag while many YouTube-sourced recordings carry none. Expect low hit rates here; stamping `genres_checked_at` ensures each file is probed once and falls cleanly into the "No genre" bucket if untagged.
   - Cost estimate: `ffprobe` reads only container headers (~10 to 30 ms per file on a local disk, ~50 to 150 ms over SMB). At 10 files per poll tick, 1,000 files finish in under 20 minutes with negligible CPU and no network traffic.

### Step C: Re-tagging existing `.m4a` files (explicitly deferred / opt-in)

Re-writing `.m4a` metadata via `ffmpeg -c:a copy -metadata genre=...` is fast locally (remux only, no audio re-encode), but it has two real operational costs:

- Every rewritten file changes bytes on disk, which invalidates any CDN copy (`mirrored_at`) if you want CDN and SMB copies to match. Re-uploading a 20 GB library to the CDN just to add a `genre` atom is rarely worth the bandwidth and egress cost.
- Touching SMB files under active playback or external indexers (Navidrome/Jellyfin) triggers full library rescans.

Recommendation: do **not** rewrite existing `.m4a` files automatically. Populate the database for immediate UI support, write the `genre` tag on all newly downloaded files going forward, and optionally provide a manual CLI tool (`scripts/retag_music_genres.py`) for operators who want existing SMB files updated.

## 5. API additions

All endpoints stay under `/music`, behind `require_music_user`, scoped to `user["user_id"]`, and excluding `pt.removed_upstream = TRUE`.

### 5.1 Extended `GET /music/all-titles`

Backwards-compatible query parameters added to the existing endpoint:

| Parameter | Type | Default | Behavior |
| --- | --- | --- | --- |
| `sort` | `title \| artist \| album \| genre \| added` | `title` | Primary sort key (`artist` uses first artist lowercased; `genre` uses alphabetically first genre with untagged tracks sorted last via `NULLS LAST`; `added` uses `MAX(pt.added_at)`). Tie-break always ends with `LOWER(t.title), t.id`. |
| `order` | `asc \| desc` | `asc` | Sort direction for the primary key (`NULLS LAST` always kept for missing values). |
| `genre` | `string` (optional) | none | Exact match on normalized genre name. |
| `no_genre` | `bool` | `false` | When `true`, returns only tracks with no rows in `music_track_genres` (the "No genre" bucket). A dedicated boolean avoids colliding with any real genre name and rejects being combined with `genre` (400). |
| `artist` | `string` (optional) | none | Exact match on normalized artist name (`artist_name_norm = LOWER(TRIM(%s))`). |
| `q` | `string` (optional, max 200) | none | Substring filter matching title, album, or any artist. |

Each track object keeps the exact `track_payload` shape plus `playlists` and adds `genres: list[str]`.

**SQL correctness note (avoiding cartesian blowup):** joining both `music_playlist_tracks` and `music_track_genres` directly in one `GROUP BY t.id` multiplies rows (`playlists x genres`). Fetch `genres` via a correlated subquery (or lateral aggregate) per track ID:

```sql
COALESCE(
    (SELECT ARRAY_AGG(tg.genre ORDER BY tg.genre)
     FROM music_track_genres tg
     WHERE tg.track_id = t.id),
    '{}'
) AS genres
```

Because `music_track_genres` has primary key `(track_id, genre)`, each lookup is a tight index range scan and keeps the playlist aggregation completely clean.

### 5.2 `GET /music/genres`

Lists genres present in the user's active library with deduplicated track counts, ordered by `track_count DESC, genre ASC`:

```json
{
  "success": true,
  "genres": [
    { "name": "indie rock", "track_count": 48 },
    { "name": "synthwave", "track_count": 21 }
  ],
  "no_genre_count": 14
}
```

### 5.3 `GET /music/artists`

Lists and searches artists across the user's active library:

- Query params: `q` (optional substring search, max 200), `genre` (optional genre filter), `sort` (`name | tracks`, default `name`), `limit` (default 50, max 200), `offset` (default 0).
- Groups by `mta.artist_name_norm`, picking the most common display spelling (`MODE() WITHIN GROUP (ORDER BY mta.artist_name)` or `MIN(mta.artist_name)`), counting `COUNT(DISTINCT t.id)`, returning a representative `cover_url` (signed via the existing cover logic from one ready/covered track), and top genres for that artist.

```json
{
  "success": true,
  "total": 142,
  "artists": [
    {
      "name": "Daft Punk",
      "track_count": 18,
      "genres": ["electronic", "french house"],
      "cover_url": "https://.../music_art/42?e=..."
    }
  ]
}
```

### Artist routing caveat

Artist names frequently contain slashes, question marks, or punctuation (for example `AC/DC`, `???`, `Tyler, The Creator`). Rather than putting raw artist names in a REST path segment like `/music/artists/{artist_name}`, keep artist filtering as a query parameter (`GET /music/all-titles?artist=AC%2FDC`, and client route `/music/artist?name=AC%2FDC`). That avoids reverse-proxy slash decoding issues in Nginx and Traefik.

## 6. Indexes and performance expectations

At Crimsonhaven's self-hosted scale (roughly 5 to 50 music users, 500 to 5,000 tracks per user, 10,000 to 50,000 unique tracks total, 2 to 5 genres per track -> 20,000 to 250,000 rows in `music_track_genres` and `music_track_artists`):

- **Existing indexes reused**: `music_playlists_uniq` already leads with `user_id`, so finding a user's playlists is an index scan. `music_playlist_tracks` primary key `(playlist_id, track_id)` serves the join from `music_playlists` to `music_playlist_tracks`.
- **New indexes**:
  - `idx_music_track_genres_genre (genre, track_id)`: filters `all-titles?genre=...` in a few milliseconds.
  - `idx_music_track_artists_norm (artist_name_norm, track_id)`: exact artist lookups and artist-page queries in <5 ms.
  - `idx_music_track_artists_name_trgm` (GIN `gin_trgm_ops`, optional): serves `ILIKE '%q%'` artist search in <5 ms without a sequential scan. Even without `pg_trgm`, a sequential scan over 50k short text rows takes ~10 to 20 ms.
- **Query timings**: expect 5 to 20 ms for `/music/all-titles`, `/music/genres`, and `/music/artists` on warm cache.
- **Payload and DOM cost (be honest here)**: returning 3,000 unpaginated tracks in one JSON payload is ~1.2 to 1.5 MB uncompressed (~150 to 200 KB gzipped), and rendering 3,000 `<ol><li>` rows starts feeling sluggish on low-end phones. Up to ~1,500 tracks, unpaginated rendering is fine and keeps `playTracks(ready, ...)` simple (one full queue). Beyond ~2,000 tracks, either add cursor/offset pagination on `all-titles` or virtualize the list in the client while keeping a lightweight queue fetch for "Play all".

## 7. Client UX

All additions should match `MusicAllTitles.jsx`, `MusicPlaylist.jsx`, and `MusicHub.jsx` (crimson Tailwind palette, `text-crimson-400` for muted text per `eslint.config.js`, no new UI libraries).

1. **Controls bar at the top of `/music/all-titles`**:
   - **Artist / track search input**: compact search field (`Search artist or title...`) that filters immediately (debounced ~200 ms if server-backed) and surfaces matching artist pills above the track list when `q` matches artists from `GET /music/artists?q=...`.
   - **Sort selector**: segmented control or `<select>` for `Title`, `Artist`, `Album`, `Genre` plus an asc/desc toggle button. Store active filters and sort in URL search params (`?sort=artist&genre=synthwave`) so reload, back navigation, and sharing links within the app preserve state.
   - **Genre filter chips**: horizontally scrollable row of pills (`All`, top genres by track count, and `No genre` when `no_genre_count > 0`). If a user has more than ~12 genres, show the top 10 inline plus a `"More genres"` button opening a `Sheet` (reusing `src/music/Sheet.jsx`) with a searchable list of all genres and their counts.
2. **Artist view (`/music/artist?name=...`)**:
   - Clicking an artist name on a track row, an artist search result, or a future "Artists" section opens `/music/artist?name=<encoded>`.
   - Header: representative `Cover`, artist name, genre chips (clickable back to `/music/all-titles?genre=...`), ready/total count, total duration, `Play` and `Shuffle` buttons wired into `playTracks(ready, ..., { name: artistName, path: '/music/artist?name=...' })` so `MusicNowPlaying` links back naturally.
   - Track list: same row layout and `StatusCell` as `MusicAllTitles`, sorted by album then title by default.
3. **Mobile layout**:
   - Genre chips scroll horizontally with hidden/subtle scrollbar instead of wrapping into five vertical rows.
   - Sort and search stack cleanly above the list on small viewports, with touch targets at least 36 to 44 px tall.

## 8. Phased rollout

1. **Phase 1: Schema and ingestion (backend only, zero UI changes)**
   - Ship the migration creating `music_track_genres`, `music_track_artists`, `music_artist_cache`, and `music_tracks.genres_checked_at`, plus the one-shot SQL backfill of `music_track_artists`.
   - Update `spotify.py` (`artists(id,name)` + batched `/v1/artists` lookup with cache), `csv_import.py` (`Artist Genres`), `db.py` (keep `music_track_artists` and `music_track_genres` in sync on write), and `tagging.py` (write `genre` metadata on new downloads).
   - Add unit tests for genre normalization, CSV genre parsing, artist cache batching, and `tag_args` genre output.
2. **Phase 2: Background genre backfill**
   - Add the rate-limited enrichment pass in `music_engine/worker.py` (Spotify batch lookup first, `ffprobe` fallback second, stamping `genres_checked_at`).
   - Monitor coverage on dev/prod (percentage of tracks with at least one genre vs "No genre") before turning on genre UI.
3. **Phase 3: Backend API**
   - Add `sort`, `order`, `genre`, `no_genre`, `artist`, and `q` to `GET /music/all-titles`.
   - Add `GET /music/genres` and `GET /music/artists`.
   - Regenerate `openapi.json` and add route/query unit tests.
4. **Phase 4: Client UX**
   - Add genre chips, sort controls, and artist search to `MusicAllTitles.jsx`, backed by URL search params and new `musicApi` helpers in `src/music/hooks.js`.
   - Add the `/music/artist` view and make artist names clickable in track rows.
   - Add Vitest coverage for query-param serialization, genre chip overflow, and artist URL helpers.
5. **Phase 5: Optional follow-ups (only if real usage asks for them)**
   - Broad parent-genre grouping if Spotify's micro-genres prove too granular.
   - Manual `.m4a` re-tag CLI script (`scripts/retag_music_genres.py`) if operators want old SMB files tagged.
   - Server pagination or client list virtualization if any member crosses ~2,000 tracks in `All titles`.
