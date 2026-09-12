-- 004_airing.sql
--
-- The airing calendar, per-title subscriptions, and the ledger that keeps a
-- notification from being sent twice.
--
-- Where the data comes from
-- -------------------------
-- metadata_engine.anilist has asked AniList for nextAiringEpisode on every title
-- fetch since the beginning and thrown it away. airing_schedule is the first
-- place it is kept. It is filled by a windowed AniList query (all anime airing
-- between a lookback and a horizon), not one request per subscription.
--
-- Timestamps are TIMESTAMPTZ rather than the ISO-TEXT the account tables use,
-- for the same reason security_events is: every query here is time arithmetic
-- (what aired since, what airs before) and lexicographic TEXT comparison cannot
-- express a window.
--
-- Why the ledger is a table and not a flag
-- ----------------------------------------
-- Sending is not transactional with recording that it was sent, so the two
-- orderings fail differently: claim-then-send can lose a notification if the
-- process dies mid-run, send-then-claim can resend the same mail on every tick
-- forever. Claiming first, with INSERT ... ON CONFLICT DO NOTHING as the claim,
-- takes the bounded failure. status then records what became of the claim, so a
-- failure is visible rather than silently indistinguishable from a success.
--
-- The primary key is (user_id, anilist_id, episode), deliberately not involving
-- time: AniList revises airingAt when a broadcast slips, and an episode already
-- notified must not be notified again because its timestamp moved.

CREATE TABLE IF NOT EXISTS airing_schedule (
    anilist_id INTEGER     NOT NULL,
    episode    INTEGER     NOT NULL,
    airing_at  TIMESTAMPTZ NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (anilist_id, episode)
);

-- The calendar reads a window, and the retention sweep deletes by age.
CREATE INDEX IF NOT EXISTS idx_airing_schedule_at ON airing_schedule(airing_at);

-- title and poster are a snapshot taken when the subscription is created, not a
-- join. A subscription may name a title the mapping sync has not seen yet, so
-- anime_entries cannot be relied on to have a row, and the alternative (an
-- AniList fetch per subscriber while sending a burst of mail) is exactly what
-- the windowed poll exists to avoid. Titles effectively never change.
CREATE TABLE IF NOT EXISTS anime_subscriptions (
    user_id      BIGINT      NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    anilist_id   INTEGER     NOT NULL,
    title        TEXT,
    poster       TEXT,
    notify_email BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, anilist_id)
);

-- The send query starts from the schedule and finds its subscribers.
CREATE INDEX IF NOT EXISTS idx_anime_subscriptions_anilist ON anime_subscriptions(anilist_id);

CREATE TABLE IF NOT EXISTS airing_notifications (
    user_id    BIGINT      NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    anilist_id INTEGER     NOT NULL,
    episode    INTEGER     NOT NULL,
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at    TIMESTAMPTZ,
    -- claimed -> sent | failed. A row is never deleted to retry: that would
    -- reopen the resend hole the claim exists to close.
    status     TEXT        NOT NULL DEFAULT 'claimed',
    PRIMARY KEY (user_id, anilist_id, episode)
);

-- For the admin view of what failed, and for the retention sweep.
CREATE INDEX IF NOT EXISTS idx_airing_notifications_claimed ON airing_notifications(claimed_at);
