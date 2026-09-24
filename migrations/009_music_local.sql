-- 009_music_local.sql
--
-- Playlists a member builds inside Crimson Haven (source 'local') out of songs
-- found by search. Such a song has no Spotify metadata behind it, only a
-- search result's title and channel, so its tags are taken from the source's
-- own music metadata when the download carries any.

ALTER TABLE music_tracks ADD COLUMN IF NOT EXISTS tags_from_source BOOLEAN NOT NULL DEFAULT FALSE;
