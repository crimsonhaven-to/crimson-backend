-- 011_music_listens.sql
--
-- One row per song a member actually listened to, so Crimson Wrapped can count
-- a year of music the way watch_events counts a year of watching.
--
-- What counts as a listen
-- -----------------------
-- The player decides, not the server: a song counts once it has played for 30
-- seconds (the rule Spotify uses), and seconds is the time it really played,
-- not how far the scrubber got. A song repeated is two rows.
--
-- listened_at comes from the device
-- ---------------------------------
-- A song played offline (a download, in a tunnel) is reported when the device
-- is next online, possibly days later, so the server cannot stamp it. The
-- route clamps it to the recent past. It is also what makes a retried report
-- harmless: the same (user, track, moment) is one row.
--
-- Growth
-- ------
-- A heavy listener is some tens of thousands of rows a year. Pruned with
-- watch_events' three years by the retention sweep.

CREATE TABLE IF NOT EXISTS music_listens (
    user_id     BIGINT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    track_id    BIGINT NOT NULL REFERENCES music_tracks(id) ON DELETE CASCADE,
    listened_at TIMESTAMPTZ NOT NULL,
    seconds     DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, track_id, listened_at)
);

-- Every read is one account over one year.
CREATE INDEX IF NOT EXISTS idx_music_listens_user_time
    ON music_listens(user_id, listened_at);
