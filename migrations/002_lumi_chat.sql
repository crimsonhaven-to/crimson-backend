-- 002_lumi_chat.sql
--
-- Lumi, the in-app chatbot: per-user access grants, operator settings,
-- conversation storage and a usage ledger.
--
-- Why a migration rather than another init_db()
-- --------------------------------------------
-- Per 000_baseline.sql, every schema change from version 0 onward lives in a
-- numbered file here. chat_engine therefore ships NO init_db() of its own; this
-- file is the whole of its schema.
--
-- Access model
-- ------------
-- chat_enabled defaults FALSE, so the feature is deny-by-default for every
-- existing and future account. An admin grants it per user from the dashboard
-- (PATCH /admin/users/{id}), exactly like the is_admin flag above it. There is
-- deliberately no env var that grants access in bulk: the operator decision
-- belongs in the dashboard where it can be audited.
--
-- Spend control
-- -------------
-- The real financial risk of an LLM feature is not the per-message price, it is
-- a runaway tool-calling loop. Two brakes:
--   * chat_monthly_token_budget on the account (NULL falls back to the global
--     default in chat_settings), enforced before each provider call,
--   * the chat_usage ledger, which records every provider call with its token
--     counts and an estimated cost so the dashboard can show real spend.
-- Both are advisory in the sense that a single in-flight request can still
-- overshoot; they stop the NEXT call, which is what bounds a runaway loop.

-- --- per-account access + budget -------------------------------------------
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS chat_enabled BOOLEAN NOT NULL DEFAULT FALSE;
-- NULL means "use the global default from chat_settings". 0 means "explicitly
-- blocked", which is distinct from NULL and lets an admin freeze one user
-- without revoking access outright.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS chat_monthly_token_budget BIGINT;

-- --- operator settings (single row, id = 1) --------------------------------
-- Provider and model are operator choices that should be changeable without a
-- redeploy, so they live here rather than in Config. API KEYS DO NOT: they stay
-- in the environment (ANTHROPIC_API_KEY / GEMINI_API_KEY) so a database dump
-- never contains billable credentials. The dashboard reports whether each key is
-- present, never its value.
CREATE TABLE IF NOT EXISTS chat_settings (
    id                       SMALLINT PRIMARY KEY DEFAULT 1,
    enabled                  BOOLEAN NOT NULL DEFAULT FALSE,
    provider                 TEXT    NOT NULL DEFAULT 'anthropic',
    model                    TEXT    NOT NULL DEFAULT 'claude-sonnet-5',
    -- Global fallback budget for accounts whose own budget is NULL.
    monthly_token_budget     BIGINT  NOT NULL DEFAULT 2000000,
    -- Turns of history replayed to the model. Caps the input token growth of a
    -- long conversation, which is the main per-message cost driver.
    history_turns            INTEGER NOT NULL DEFAULT 12,
    -- Hard ceiling on tool round trips within a single user message, so a model
    -- that decides to search forever cannot bill forever.
    max_tool_iterations      INTEGER NOT NULL DEFAULT 5,
    updated_at               TEXT,
    updated_by               BIGINT,
    CONSTRAINT chat_settings_singleton CHECK (id = 1)
);

INSERT INTO chat_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- --- conversations ---------------------------------------------------------
-- One row per chat thread. Threads are pruned after 30 days by the scheduled
-- job in chat_engine.db (registered on the RUN_DB_SYNC replica alongside the
-- other maintenance jobs), so the table stays small without manual care.
CREATE TABLE IF NOT EXISTS chat_conversations (
    conversation_id BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    title           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_conv_user ON chat_conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_conv_pruning ON chat_conversations(updated_at);

-- --- messages --------------------------------------------------------------
-- role is 'user' | 'assistant'. Tool traffic is NOT persisted as its own row:
-- the tool results are regenerated from live data on replay anyway, and keeping
-- them would replay stale recommendations back into the model. What IS kept is
-- the assistant's rendered text plus a small JSON payload of any action cards
-- (resolved deep links, recommendation lists) so a reloaded thread still shows
-- its buttons.
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id      BIGSERIAL PRIMARY KEY,
    conversation_id BIGINT NOT NULL REFERENCES chat_conversations(conversation_id) ON DELETE CASCADE,
    user_id         BIGINT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    role            TEXT   NOT NULL,
    content         TEXT   NOT NULL,
    actions         TEXT,
    created_at      TEXT   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_conv ON chat_messages(conversation_id, message_id);

-- --- usage ledger ----------------------------------------------------------
-- One row per provider call (a single user message with two tool round trips
-- writes three rows). cost_micros is USD millionths, computed from the model's
-- published per-million rates at call time, so the dashboard can total spend
-- without the frontend needing a price table.
CREATE TABLE IF NOT EXISTS chat_usage (
    usage_id        BIGSERIAL PRIMARY KEY,
    user_id         BIGINT NOT NULL REFERENCES accounts(user_id) ON DELETE CASCADE,
    provider        TEXT   NOT NULL,
    model           TEXT   NOT NULL,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cached_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_micros     BIGINT  NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_usage_user ON chat_usage(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_chat_usage_created ON chat_usage(created_at);
