-- ============================================================
-- Nova AI Assistant — schema_v2.sql
--
-- MIGRATION from schema.sql (v1) → v2.
-- Purpose: add per-user isolation to conversations.
--
-- Run this in your Supabase project:
--   Dashboard → SQL Editor → paste → Run
--
-- What changed from v1:
--   1. conversations.user_id  — links each conversation to a Supabase
--      auth user (UUID FK → auth.users.id).
--   2. RLS policies are replaced with per-user policies so that
--      User A can never read or write User B's conversations or messages.
--   3. messages and action_logs derive user isolation via the
--      conversation FK — no extra user_id column needed.
--   4. Index on (user_id, updated_at) for fast per-user sidebar list.
--
-- If you already ran schema.sql:
--   The ALTER TABLE blocks add the column to existing tables.
--   The DROP POLICY blocks replace the v1 permissive policies.
--
-- If running fresh (new project):
--   The IF NOT EXISTS guards make all CREATE TABLE blocks safe.
-- ============================================================

-- ── Enable UUID generation ─────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ============================================================
-- profiles  (unchanged from profile_schema.sql — idempotent)
-- ============================================================
CREATE TABLE IF NOT EXISTS profiles (
    id          UUID        PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    full_name   TEXT,
    email       TEXT,
    avatar_url  TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION update_profiles_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_profiles_updated_at ON profiles;
CREATE TRIGGER trg_profiles_updated_at
    BEFORE UPDATE ON profiles
    FOR EACH ROW EXECUTE FUNCTION update_profiles_updated_at();

ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "profiles_select_own"  ON profiles;
DROP POLICY IF EXISTS "profiles_insert_own"  ON profiles;
DROP POLICY IF EXISTS "profiles_update_own"  ON profiles;

CREATE POLICY "profiles_select_own" ON profiles
    FOR SELECT USING (auth.uid() = id);
CREATE POLICY "profiles_insert_own" ON profiles
    FOR INSERT WITH CHECK (auth.uid() = id);
CREATE POLICY "profiles_update_own" ON profiles
    FOR UPDATE USING (auth.uid() = id) WITH CHECK (auth.uid() = id);


-- ============================================================
-- conversations
-- ============================================================
CREATE TABLE IF NOT EXISTS conversations (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    title       TEXT        NOT NULL DEFAULT 'Nova Chat',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- If migrating from v1 (table already exists without user_id):
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'conversations' AND column_name = 'user_id'
    ) THEN
        -- Add user_id; existing rows get NULL (safe during migration)
        ALTER TABLE conversations ADD COLUMN user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE;
        -- Make it NOT NULL after backfill (skip backfill for existing dev data)
        -- In a real migration you'd backfill then: ALTER TABLE conversations ALTER COLUMN user_id SET NOT NULL;
    END IF;
END $$;

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_conversations_updated_at ON conversations;
CREATE TRIGGER trg_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Indexes
CREATE INDEX IF NOT EXISTS idx_conversations_user_updated
    ON conversations (user_id, updated_at DESC);


-- ============================================================
-- messages  (no schema change needed — isolation via conversation FK)
-- ============================================================
CREATE TABLE IF NOT EXISTS messages (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT        NOT NULL CHECK (role IN ('user', 'bot', 'system')),
    content         TEXT        NOT NULL,
    tool_intent     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages (conversation_id, created_at ASC);


-- ============================================================
-- action_logs  (unchanged)
-- ============================================================
CREATE TABLE IF NOT EXISTS action_logs (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID        REFERENCES conversations(id) ON DELETE SET NULL,
    intent          TEXT        NOT NULL,
    tool_type       TEXT        NOT NULL CHECK (tool_type IN ('browser', 'android')),
    status          TEXT        NOT NULL CHECK (status IN (
                                    'dispatched', 'success', 'failed',
                                    'cancelled', 'permission_denied')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_action_logs_conversation
    ON action_logs (conversation_id, created_at DESC);


-- ============================================================
-- Row Level Security — per-user isolation
--
-- Security model:
--   • conversations: only the owner (user_id = auth.uid()) can
--     SELECT, INSERT, UPDATE, DELETE their own rows.
--   • messages: access is allowed only when the parent conversation
--     belongs to the current user.  No extra user_id column needed —
--     the FK join enforces isolation cleanly.
--   • action_logs: same join-based check via conversations.
--   • The backend uses the anon key with Supabase Auth JWT.
--     auth.uid() returns the user's UUID from the JWT automatically.
--
-- Why this is sufficient:
--   Even if a user guesses another user's conversation UUID, the
--   SELECT policy on messages requires the conversation to belong
--   to auth.uid() — so the query returns zero rows (not an error,
--   which would leak existence).
-- ============================================================

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages      ENABLE ROW LEVEL SECURITY;
ALTER TABLE action_logs   ENABLE ROW LEVEL SECURITY;

-- Drop v1 permissive policies
DROP POLICY IF EXISTS "allow_all_conversations" ON conversations;
DROP POLICY IF EXISTS "allow_all_messages"      ON messages;
DROP POLICY IF EXISTS "allow_all_action_logs"   ON action_logs;

-- ── conversations: owner-only ──────────────────────────────────
CREATE POLICY "conv_select_own" ON conversations
    FOR SELECT USING (user_id = auth.uid());

CREATE POLICY "conv_insert_own" ON conversations
    FOR INSERT WITH CHECK (user_id = auth.uid());

CREATE POLICY "conv_update_own" ON conversations
    FOR UPDATE USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

CREATE POLICY "conv_delete_own" ON conversations
    FOR DELETE USING (user_id = auth.uid());

-- ── messages: readable/writable only via own conversations ────
-- Using EXISTS subquery keeps isolation enforced at DB level.
CREATE POLICY "msg_select_own" ON messages
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.id = messages.conversation_id
              AND c.user_id = auth.uid()
        )
    );

CREATE POLICY "msg_insert_own" ON messages
    FOR INSERT WITH CHECK (
        EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.id = messages.conversation_id
              AND c.user_id = auth.uid()
        )
    );

CREATE POLICY "msg_delete_own" ON messages
    FOR DELETE USING (
        EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.id = messages.conversation_id
              AND c.user_id = auth.uid()
        )
    );

-- ── action_logs: same pattern ─────────────────────────────────
CREATE POLICY "log_select_own" ON action_logs
    FOR SELECT USING (
        conversation_id IS NULL OR EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.id = action_logs.conversation_id
              AND c.user_id = auth.uid()
        )
    );

CREATE POLICY "log_insert_own" ON action_logs
    FOR INSERT WITH CHECK (
        conversation_id IS NULL OR EXISTS (
            SELECT 1 FROM conversations c
            WHERE c.id = action_logs.conversation_id
              AND c.user_id = auth.uid()
        )
    );


-- ============================================================
-- Verify
-- ============================================================
SELECT
    t.table_name,
    array_agg(c.column_name ORDER BY c.ordinal_position) AS columns
FROM information_schema.tables t
JOIN information_schema.columns c
    ON c.table_name = t.table_name AND c.table_schema = 'public'
WHERE t.table_schema = 'public'
  AND t.table_name IN ('conversations', 'messages', 'action_logs', 'profiles')
GROUP BY t.table_name
ORDER BY t.table_name;
