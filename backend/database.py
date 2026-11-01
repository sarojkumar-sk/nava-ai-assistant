"""
Nova AI Assistant — database.py

Safe Supabase client initialisation.

Key selection (in priority order):
  1. SUPABASE_SERVICE_KEY (service-role key) — bypasses RLS entirely.
     Best for a trusted backend server. Get it from:
     Supabase Dashboard → Settings → API → service_role (secret) key.
  2. SUPABASE_KEY (anon key) — subject to RLS policies.
     Only works correctly if the RLS policies allow the anon role
     (see backend/schema_v4.sql which fixes this).

Rules:
- NEVER crashes at import time, even if env vars are missing.
- get_db() returns a live Supabase client or None (caller decides what to do).
- is_available() is a cheap boolean check — use it before calling get_db().
- The client is created once (lazy singleton) and reused.

Graceful degradation:
  If Supabase credentials are not set, all memory operations silently no-op.
  The app still works — it just won't persist conversation history.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("nova.database")

# ── Module-level state ─────────────────────────────────────────
_client = None          # Supabase client singleton
_init_attempted = False # True after first init attempt (success or fail)


def _get_key() -> str:
    """
    Return the best available Supabase key.

    Priority order:
      1. SUPABASE_SERVICE_KEY       — preferred name (bypasses RLS)
      2. SUPABASE_SERVICE_ROLE_KEY  — legacy alias for the same key
      3. SUPABASE_KEY               — anon key (requires schema_v4.sql RLS fix)

    The service-role key is the correct choice for a trusted backend server;
    it bypasses RLS entirely and lets application-level .eq("user_id") checks
    enforce per-user isolation.
    """
    # Option 1: canonical service-role key name
    service_key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    if service_key:
        logger.info("Supabase: using service-role key via SUPABASE_SERVICE_KEY (RLS bypassed)")
        return service_key

    # Option 2: legacy alias (older .env files used this name)
    legacy_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if legacy_key:
        logger.info("Supabase: using service-role key via SUPABASE_SERVICE_ROLE_KEY (RLS bypassed)")
        return legacy_key

    # Option 3: anon key — works only when schema_v4.sql has been applied
    anon_key = os.getenv("SUPABASE_KEY", "").strip()
    if anon_key:
        logger.info(
            "Supabase: using anon key (SUPABASE_KEY). "
            "RLS policies must allow the anon role — run schema_v4.sql in "
            "Supabase SQL Editor if messages are not being saved."
        )
        return anon_key

    return ""


def is_available() -> bool:
    """
    Returns True if Supabase credentials are configured in env.
    Does NOT attempt to connect — just checks that the vars are set.

    Accepts any of the three key env vars:
      SUPABASE_SERVICE_KEY      — preferred service-role key name
      SUPABASE_SERVICE_ROLE_KEY — legacy alias for service-role key
      SUPABASE_KEY              — anon key (requires schema_v4.sql)
    """
    return bool(
        os.getenv("SUPABASE_URL", "").strip() and
        (
            os.getenv("SUPABASE_SERVICE_KEY",      "").strip() or
            os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip() or
            os.getenv("SUPABASE_KEY",              "").strip()
        )
    )


def get_db():
    """
    Return a live Supabase client, or None if unavailable.

    On first call:
      - If credentials are missing, logs a warning and returns None.
      - If credentials are present, attempts to create the client.
      - On import error (package not installed), logs an error and returns None.
      - On any other exception, logs and returns None.

    Subsequent calls return the cached client (or None if init failed).
    """
    global _client, _init_attempted

    if _init_attempted:
        return _client

    _init_attempted = True

    if not is_available():
        logger.warning(
            "Supabase not configured — SUPABASE_URL and a key (SUPABASE_SERVICE_KEY "
            "or SUPABASE_KEY) are required. "
            "Conversation history will not be saved. "
            "Set them in backend/.env to enable memory."
        )
        return None

    try:
        from supabase import create_client, Client  # noqa: F401
        url = os.getenv("SUPABASE_URL").strip()
        key = _get_key()
        _client = create_client(url, key)
        logger.info("Supabase client initialised OK (url=%s…)", url[:30])
    except ImportError:
        logger.error(
            "supabase package is not installed. "
            "Run: pip install supabase==2.31.0"
        )
    except Exception as e:
        logger.error("Supabase client init failed: %s", str(e))

    return _client


def reset_for_testing() -> None:
    """
    Reset the singleton — useful in tests only.
    Not called in production code.
    """
    global _client, _init_attempted
    _client = None
    _init_attempted = False
