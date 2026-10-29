-- ============================================================
-- Nova AI Assistant — schema_v3.sql
--
-- ADDITIVE migration: adds the user_memories table.
-- Run this AFTER schema_v2.sql in your Supabase project:
--   Dashboard → SQL Editor → paste → Run
--
-- What this adds:
--   • user_memories — per-user fact/preference store
--
-- What is NOT changed:
--   • conversations, messages, action_logs, profiles
--     (schema_v2 tables are untouched)
--
-- Security model:
--   • RLS is ON with strict per-user policies (no allow_all shortcut).
--   • auth.uid() is always the gating condition — users can only
--     read/write/delete their own rows.
--   • The backend uses the Supabase anon key + JWT so auth.uid()
--     is populated from the user's access token automatically.
--   • No secret-detection column — secrets must not be stored
--     (enforced by application-layer validation in user_memory.py).
-- ============================================================

-- ── Enable UUID generation (idempotent) ───────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ============================================================
-- user_memories
--
-- One row = one remembered fact or preference for one user.
--
-- key    : normalised slug (lowercase, underscores, max 80 chars)
--          e.g. "favourite_color", "home_city", "wake_up_time"
--          UNIQUE per user — upsert on conflict updates the value.
--
-- value  : the remembered fact, plain text, max 500 chars.
--          Application layer rejects anything that looks like
--          a password, token, or private key before it reaches DB.
--
-- source : how the memory was created — 'agent' (Nova decided to
--          remember), 'explicit' (user said "remember that…"),
--          or 'api' (direct API call).
--
-- UNIQUE (user_id, key) enforces one canonical slot per label
-- per user.  Inserting a duplicate key runs an upsert so the
-- value is updated, not duplicated.
-- ============================================================
CREATE TABLE IF NOT EXISTS user_memories (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID        NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    key         TEXT        NOT NULL,
    value       TEXT        NOT NULL,
    source      TEXT        NOT NULL DEFAULT 'agent'
                            CHECK (source IN ('agent', 'explicit', 'api')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- One canonical slot per (user, key)
    CONSTRAINT uq_user_memory_key UNIQUE (user_id, key),

    -- Key format: lowercase letters, digits, underscores, hyphens only
    CONSTRAINT ck_memory_key_format
        CHECK (key ~ '^[a-z0-9_-]{1,80}$'),

    -- Value must not be empty and must not exceed 500 chars
    CONSTRAINT ck_memory_value_length
        CHECK (char_length(value) BETWEEN 1 AND 500)
);

-- ── Trigger: auto-update updated_at ───────────────────────────
CREATE OR REPLACE FUNCTION update_user_memories_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_user_memories_updated_at ON user_memories;
CREATE TRIGGER trg_user_memories_updated_at
    BEFORE UPDATE ON user_memories
    FOR EACH ROW EXECUTE FUNCTION update_user_memories_updated_at();

-- ── Indexes ────────────────────────────────────────────────────
-- Fast lookup of all memories for one user (RECALL_MEMORY)
CREATE INDEX IF NOT EXISTS idx_user_memories_user
    ON user_memories (user_id, updated_at DESC);

-- Fast lookup of one memory by key (DELETE_MEMORY + targeted recall)
CREATE INDEX IF NOT EXISTS idx_user_memories_user_key
    ON user_memories (user_id, key);


-- ============================================================
-- Row Level Security
--
-- All four operations restricted to the memory's own user.
-- auth.uid() is populated from the Supabase JWT automatically.
-- No service-role bypass — anon key + JWT is sufficient.
-- ============================================================
ALTER TABLE user_memories ENABLE ROW LEVEL SECURITY;

-- SELECT: only your own memories
CREATE POLICY "mem_select_own" ON user_memories
    FOR SELECT
    USING (user_id = auth.uid());

-- INSERT: you can only insert memories for yourself
CREATE POLICY "mem_insert_own" ON user_memories
    FOR INSERT
    WITH CHECK (user_id = auth.uid());

-- UPDATE: you can only update your own memories
CREATE POLICY "mem_update_own" ON user_memories
    FOR UPDATE
    USING (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- DELETE: you can only delete your own memories
CREATE POLICY "mem_delete_own" ON user_memories
    FOR DELETE
    USING (user_id = auth.uid());


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
  AND t.table_name = 'user_memories'
GROUP BY t.table_name;

-- Verify RLS is enabled and policies exist
SELECT schemaname, tablename, policyname, cmd, qual
FROM pg_policies
WHERE tablename = 'user_memories'
ORDER BY policyname;
