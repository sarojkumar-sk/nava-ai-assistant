-- ============================================================
-- Nova AI Assistant — profile_schema.sql
--
-- Run this AFTER the base schema.sql in your Supabase project:
--   Dashboard → SQL Editor → paste → Run
--
-- Purpose:
--   Stores the public profile of each authenticated user.
--   Populated automatically on first login via the backend
--   /auth/me endpoint.
--
-- Security:
--   - RLS is ON: users can only read/write their own row.
--   - id is the Supabase auth.users UUID — no separate sequence.
--   - No passwords stored here; auth is handled by Supabase Auth.
--   - avatar_url is a public Google CDN URL (no personal data risk).
-- ============================================================

-- ── Enable UUID generation (idempotent) ───────────────────────
CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ============================================================
-- profiles
-- ============================================================
CREATE TABLE IF NOT EXISTS profiles (
    -- Primary key = Supabase auth.users.id (UUID)
    -- This links the profile to the authenticated user without
    -- a separate join table.
    id          UUID        PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,

    -- Basic profile fields from Google OAuth
    -- (openid + profile + email scope only — no Gmail/Drive/Contacts)
    full_name   TEXT,
    email       TEXT,
    avatar_url  TEXT,

    -- Timestamps
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Auto-update updated_at on every change
CREATE OR REPLACE FUNCTION update_profiles_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_profiles_updated_at
    BEFORE UPDATE ON profiles
    FOR EACH ROW EXECUTE FUNCTION update_profiles_updated_at();


-- ============================================================
-- Row Level Security
-- ============================================================
ALTER TABLE profiles ENABLE ROW LEVEL SECURITY;

-- Users can only read their own profile row
CREATE POLICY "profiles_select_own"
    ON profiles
    FOR SELECT
    USING (auth.uid() = id);

-- Users can only insert their own profile row
CREATE POLICY "profiles_insert_own"
    ON profiles
    FOR INSERT
    WITH CHECK (auth.uid() = id);

-- Users can only update their own profile row
CREATE POLICY "profiles_update_own"
    ON profiles
    FOR UPDATE
    USING (auth.uid() = id)
    WITH CHECK (auth.uid() = id);

-- No DELETE policy — users cannot delete their own profile
-- (prevents accidental data loss; soft-delete via Supabase Auth)


-- ============================================================
-- Helper: upsert_profile function (called by backend on login)
-- Uses SECURITY DEFINER so it runs as the table owner,
-- bypassing RLS for the insert — safe because the function
-- validates that id = auth.uid() before upserting.
-- ============================================================
CREATE OR REPLACE FUNCTION upsert_profile(
    p_id        UUID,
    p_full_name TEXT,
    p_email     TEXT,
    p_avatar    TEXT
)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    -- Only allow upserting your own profile
    IF p_id != auth.uid() THEN
        RAISE EXCEPTION 'Unauthorized: cannot upsert another user''s profile';
    END IF;

    INSERT INTO profiles (id, full_name, email, avatar_url)
    VALUES (p_id, p_full_name, p_email, p_avatar)
    ON CONFLICT (id) DO UPDATE
        SET full_name  = EXCLUDED.full_name,
            email      = EXCLUDED.email,
            avatar_url = EXCLUDED.avatar_url,
            updated_at = now();
END;
$$;


-- ============================================================
-- Verify
-- ============================================================
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name = 'profiles';
