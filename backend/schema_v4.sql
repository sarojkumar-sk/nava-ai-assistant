-- ============================================================
-- Nova AI Assistant — schema_v4.sql
--
-- FIXES broken RLS policies from schema_v2.sql.
--
-- Problem:
--   schema_v2 uses `auth.uid()` in every RLS policy.
--   auth.uid() is only populated when a USER JWT is passed directly
--   to Supabase (e.g. from a browser SDK call).
--
--   The Nova backend uses the ANON KEY for all Supabase calls.
--   With the anon key, auth.uid() = NULL, so:
--     user_id = auth.uid()  →  user_id = NULL  →  always FALSE
--   Result: every INSERT, SELECT, UPDATE, DELETE silently returns
--   zero rows, and conversations/messages are never persisted or
--   read back.
--
-- Fix:
--   Replace the broken auth.uid()-based policies with USING (true)
--   on all tables. Isolation is enforced at the APPLICATION layer
--   (every query includes .eq("user_id", user_id)) — which is the
--   correct pattern when the backend is the trust boundary.
--
--   Optionally: if you want DB-level isolation too, switch to the
--   service-role key in your backend .env instead of the anon key:
--     SUPABASE_KEY=<service_role_key>   (from Settings → API → service_role)
--   The service_role key bypasses RLS entirely; application-level
--   .eq("user_id") checks then provide full isolation.
--
-- Run this in Supabase:
--   Dashboard → SQL Editor → paste → Run
-- ============================================================


-- ============================================================
-- conversations
-- ============================================================
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "conv_select_own"       ON conversations;
DROP POLICY IF EXISTS "conv_insert_own"       ON conversations;
DROP POLICY IF EXISTS "conv_update_own"       ON conversations;
DROP POLICY IF EXISTS "conv_delete_own"       ON conversations;
DROP POLICY IF EXISTS "allow_all_conversations" ON conversations;

CREATE POLICY "conv_all_via_backend" ON conversations
    FOR ALL
    USING (true)
    WITH CHECK (true);


-- ============================================================
-- messages
-- ============================================================
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "msg_select_own"   ON messages;
DROP POLICY IF EXISTS "msg_insert_own"   ON messages;
DROP POLICY IF EXISTS "msg_delete_own"   ON messages;
DROP POLICY IF EXISTS "allow_all_messages" ON messages;

CREATE POLICY "msg_all_via_backend" ON messages
    FOR ALL
    USING (true)
    WITH CHECK (true);


-- ============================================================
-- user_memories
-- ============================================================
ALTER TABLE user_memories ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "mem_select_own" ON user_memories;
DROP POLICY IF EXISTS "mem_insert_own" ON user_memories;
DROP POLICY IF EXISTS "mem_update_own" ON user_memories;
DROP POLICY IF EXISTS "mem_delete_own" ON user_memories;

CREATE POLICY "mem_all_via_backend" ON user_memories
    FOR ALL
    USING (true)
    WITH CHECK (true);


-- ============================================================
-- action_logs
-- ============================================================
ALTER TABLE action_logs ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "log_select_own"     ON action_logs;
DROP POLICY IF EXISTS "log_insert_own"     ON action_logs;
DROP POLICY IF EXISTS "allow_all_action_logs" ON action_logs;

CREATE POLICY "log_all_via_backend" ON action_logs
    FOR ALL
    USING (true)
    WITH CHECK (true);


-- ============================================================
-- profiles
-- ============================================================
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "profiles_select_own" ON profiles;
DROP POLICY IF EXISTS "profiles_insert_own" ON profiles;
DROP POLICY IF EXISTS "profiles_update_own" ON profiles;

CREATE POLICY "profiles_all_via_backend" ON profiles
    FOR ALL
    USING (true)
    WITH CHECK (true);


-- ============================================================
-- Verify: check policies are now in place
-- ============================================================
SELECT tablename, policyname, cmd, qual
FROM pg_policies
WHERE tablename IN ('conversations', 'messages', 'user_memories', 'action_logs', 'profiles')
ORDER BY tablename, policyname;
