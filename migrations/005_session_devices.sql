-- 005_session_devices.sql
--
-- Give a session enough identity for its owner to recognise it.
--
-- Until now `sessions` was (token_hash, user_id, created_at, expires_at), which
-- can only tell a user "there is a session, made at some time". That is not
-- enough to answer the question the endpoint exists for: is one of these not me?
-- user_agent and ip are captured at sign-in, last_seen_at is touched by the
-- login wall's validity cache at most once per session per TTL.
--
-- Columns are TEXT, not TIMESTAMPTZ, because the rest of this table is ISO-8601
-- TEXT and `expires_at` comparisons elsewhere rely on that (account_engine/db.py
-- documents why). A mixed-type sessions table would be worse than a consistent
-- one.
--
-- Rows that predate this migration keep NULLs. They render as an unknown device
-- rather than being hidden: a session you cannot identify is exactly the one a
-- user most needs to see in the list.

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_agent   TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ip           TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_seen_at TEXT;

-- The user-facing events endpoint filters on user_id and orders by time. The
-- existing indexes are on ts and (event_type, ts), neither of which serves it.
CREATE INDEX IF NOT EXISTS idx_secevents_user_ts ON security_events(user_id, ts DESC);
