-- ============================================================
-- Nova AI Assistant — schema.sql
--
-- Run this in your Supabase project:
--   Dashboard → SQL Editor → paste → Run
--
-- Tables:
--   conversations  — one row per chat session
--   messages       — individual user/bot messages
--   action_logs    — non-sensitive tool call log
--
-- Design decisions:
--   - No users table: Nova is a single-owner personal assistant.
--     All rows belong to the one owner. Add a user_id column later
--     if you ever need multi-user support.
--   - No PII in action_logs: only intent name + success/fail + timestamp.
--   - Row Level Security (RLS) is enabled on all tables.
--     The anon key can only read/write its own rows once you add a
--     Supabase Auth user. For a personal project, you may simply
--     disable RLS and use the anon key from a trusted backend.
-- ============================================================

-- ── Enable UUID generation ─────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ============================================================
-- conversations
-- ============================================================
CREATE TABLE IF NOT EXISTS conversations (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    title        TEXT        NOT NULL DEFAULT 'Nova Chat',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Optional: tag conversations (e.g. 'work', 'personal')
    tags         TEXT[]      DEFAULT '{}'
);

-- Auto-update updated_at on every change
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Index: fetch conversations sorted by most recent first
CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
    ON conversations (updated_at DESC);


-- ============================================================
-- messages
-- ============================================================
CREATE TABLE IF NOT EXISTS messages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'bot', 'system')),
    content         TEXT        NOT NULL,
    -- tool_intent is set only when the bot triggered a tool (e.g. 'PLAY_YOUTUBE')
    -- It is the intent name only — NOT the parameters (which may contain PII)
    tool_intent     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index: fetch messages for a conversation in order
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages (conversation_id, created_at ASC);

-- Limit messages per conversation to prevent unbounded growth
-- (this is a soft cap — enforced by the application layer, not DB)
-- The application will only fetch the last N messages for context.


-- ============================================================
-- action_logs
-- Non-sensitive tool call log — intent name + success/fail only.
-- DO NOT store: phone numbers, URLs, search queries, personal data.
-- ============================================================
CREATE TABLE IF NOT EXISTS action_logs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        REFERENCES conversations(id) ON DELETE SET NULL,
    intent          TEXT        NOT NULL,       -- e.g. 'PLAY_YOUTUBE', 'GET_WEATHER'
    tool_type       TEXT        NOT NULL CHECK (tool_type IN ('browser', 'android')),
    status          TEXT        NOT NULL CHECK (status IN ('dispatched', 'success', 'failed', 'cancelled', 'permission_denied')),
    -- Never store: parameters, URLs, phone numbers, contact names, queries
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_action_logs_conversation
    ON action_logs (conversation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_action_logs_intent
    ON action_logs (intent, created_at DESC);


-- ============================================================
-- Row Level Security (RLS)
-- For a single-owner personal project: enable RLS but add a
-- policy that allows the service role (backend) full access.
-- The anon key is used from the backend — trust the backend,
-- not individual browser clients.
-- ============================================================

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages      ENABLE ROW LEVEL SECURITY;
ALTER TABLE action_logs   ENABLE ROW LEVEL SECURITY;

-- Allow full access from authenticated role (your backend uses anon key
-- which maps to the authenticated role when RLS is bypassed with service key,
-- OR you can simply create a permissive policy for development):

-- DEVELOPMENT ONLY: allow all operations (remove in production)
CREATE POLICY "allow_all_conversations" ON conversations FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "allow_all_messages"      ON messages      FOR ALL USING (true) WITH CHECK (true);
CREATE POLICY "allow_all_action_logs"   ON action_logs   FOR ALL USING (true) WITH CHECK (true);


-- ============================================================
-- Verify
-- ============================================================
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('conversations', 'messages', 'action_logs')
ORDER BY table_name;
