-- 006_watch_events.sql
--
-- An append-only record of what was watched on which day, so a year in review
-- can be computed from when things happened rather than from when a row was
-- last touched.
--
-- Why watch_progress cannot answer the question
-- ---------------------------------------------
-- watch_progress is keyed (user_id, item_key) and upsert_progress overwrites the
-- row on a timer during playback. So updated_at is the last touch, not the watch:
-- an episode watched in January and resumed in December reads as December, and a
-- rewatch overwrites rather than appends. Distinct-episode and distinct-show
-- counts off that table are sound; anything involving a date is not.
--
-- One row per episode per day
-- ---------------------------
-- The unique key is (user_id, item_key, watched_on), so a progress ping every
-- 30 seconds writes one row the first time and updates it thereafter, not 120
-- rows an hour. seconds carries the furthest position reached that day, which is
-- the closest honest answer to "how long did you watch" that this data supports.
--
-- watched_on is UTC
-- -----------------
-- Not server-local. "Busiest day" and "longest streak" are the two stats that
-- depend on where the viewer is, and storing a local date bakes in whatever
-- timezone the API replica happened to have. The read endpoint takes a UTC
-- offset instead, so the answer stays correct when the viewer travels.
--
-- Growth
-- ------
-- A heavy viewer is a few thousand rows a year. That is small, but unbounded in
-- time, and this is the first table here whose whole purpose is to be read years
-- later. purge_expired() prunes at WATCH_EVENTS_RETENTION_DAYS (three years).

CREATE TABLE IF NOT EXISTS watch_events (
    user_id       BIGINT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    item_key      TEXT   NOT NULL,
    watched_on    DATE   NOT NULL,
    anilist_id    INTEGER,
    tmdb_id       INTEGER,
    media_type    TEXT,
    title         TEXT,
    seconds       DOUBLE PRECISION,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, item_key, watched_on)
);

-- Every read is one account over one year.
CREATE INDEX IF NOT EXISTS idx_watch_events_user_day
    ON watch_events(user_id, watched_on);
