-- 015_music_genres.sql
--
-- Genres for the music library, so members can filter and search by them.
--
-- Spotify keeps genres on artists, not on tracks, so a track records the
-- Spotify ids of its artists and the genres live once per artist in
-- music_artists, shared by every track and every member. A track's own
-- genres column holds what an import carried for that track alone (an
-- Exportify CSV). What a member sees is the union of both, computed when the
-- library is read, so a genre arriving later never leaves a stale copy behind.

ALTER TABLE music_tracks
    ADD COLUMN IF NOT EXISTS spotify_artist_ids JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS genres JSONB NOT NULL DEFAULT '[]',
    -- Set once a track without artist ids had its first artist looked up by
    -- name, found or not, so a lookup that found nothing is not repeated forever.
    ADD COLUMN IF NOT EXISTS artists_checked_at TEXT;

-- One row per Spotify artist ever looked up. An artist Spotify knows no genre
-- for keeps an empty list, which is also what stops it being asked for again.
CREATE TABLE IF NOT EXISTS music_artists (
    spotify_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    genres     JSONB NOT NULL DEFAULT '[]',
    fetched_at TEXT NOT NULL
);

-- Tracks imported before this migration have no artist ids. Their playlists
-- read Spotify again on the worker's next sync check, which brings them.
UPDATE music_playlists SET snapshot_id = NULL, last_synced_at = NULL
WHERE source = 'spotify';
