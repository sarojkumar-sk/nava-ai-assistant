"""
Nova AI Assistant — database.py

Safe Supabase client initialisation.

Rules:
- NEVER crashes at import time, even if env vars are missing.
- get_db() returns a live Supabase client or None (caller decides what to do).
- is_available() is a cheap boolean check — use it before calling get_db().
- The client is created once (lazy singleton) and reused.
- SUPABASE_URL and SUPABASE_KEY (anon key) are the only secrets needed.
  The service-role key is NEVER used here — anon key + Row Level Security (RLS)
  is the correct pattern for a backend that serves one owner.

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


def is_available() -> bool:
    """
    Returns True if Supabase credentials are configured in env.
    Does NOT attempt to connect — just checks that the vars are set.
    """
    return bool(
        os.getenv("SUPABASE_URL", "").strip() and
        os.getenv("SUPABASE_KEY", "").strip()
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
            "Supabase not configured — SUPABASE_URL or SUPABASE_KEY missing. "
            "Conversation history will not be saved. "
            "Set them in backend/.env to enable memory."
        )
        return None

    try:
        from supabase import create_client, Client  # noqa: F401
        url = os.getenv("SUPABASE_URL").strip()
        key = os.getenv("SUPABASE_KEY").strip()
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
