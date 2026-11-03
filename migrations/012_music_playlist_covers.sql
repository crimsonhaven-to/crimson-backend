-- 012_music_playlist_covers.sql
--
-- A cover a member uploads for one of their playlists. It wins over the cover
-- an import brought along, and an import's sync never touches it. Kept in the
-- database rather than on the share: the client shrinks it to a few hundred
-- kilobytes first, and a playlist is not a file on the share the way a song is.

CREATE TABLE IF NOT EXISTS music_playlist_covers (
    playlist_id  BIGINT PRIMARY KEY REFERENCES music_playlists(id) ON DELETE CASCADE,
    content_type TEXT NOT NULL,
    data         BYTEA NOT NULL,
    updated_at   TEXT NOT NULL
);
