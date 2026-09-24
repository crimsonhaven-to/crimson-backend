-- 010_music_cdn.sql
--
-- The optional off-site copy of the library (MUSIC_CDN_URL). mirrored_at is
-- set once a ready track's audio and cover are in the bucket, under the same
-- relative paths as on the share, and cleared whenever the track is
-- downloaded again. Players stream a mirrored track from the CDN.

ALTER TABLE music_tracks ADD COLUMN IF NOT EXISTS mirrored_at TEXT;
CREATE INDEX IF NOT EXISTS music_tracks_unmirrored
    ON music_tracks (id) WHERE status = 'ready' AND mirrored_at IS NULL;
