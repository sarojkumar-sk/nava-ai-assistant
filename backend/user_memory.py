"""
Nova AI Assistant — user_memory.py

Per-user fact/preference memory backed by Supabase (user_memories table).

Design principles:
  - Every operation is scoped strictly to user_id — callers MUST pass
    the authenticated user's sub from the JWT. Never accept user_id
    from the request body.
  - Secrets are rejected before they reach the DB: any value that
    looks like a password, API key, or private key is refused.
  - Keys are normalised to lowercase slugs. The DB also enforces this
    via a CHECK constraint, but we normalise first for a friendlier
    error message.
  - All DB calls are wrapped in try/except — failures are logged and
    return a safe default rather than raising. Callers never crash.

Public API:
  save_memory(user_id, key, value, source) → MemoryResult
  recall_memories(user_id, key=None)       → list[MemoryRow]
  delete_memory(user_id, key)              → MemoryResult   (one key)
  delete_all_memories(user_id)             → MemoryResult   (wipe all)
  format_memories_for_prompt(memories)     → str            (LLM context)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from database import get_db

logger = logging.getLogger("nova.user_memory")

# ── Constants ──────────────────────────────────────────────────
MAX_KEY_LEN      = 80
MAX_VALUE_LEN    = 500
MAX_MEMORIES     = 100   # hard cap per user — prevents runaway growth
KEY_PATTERN      = re.compile(r'^[a-z0-9_-]{1,80}$')

# Patterns that indicate a secret — rejected before storage.
# Deliberately simple: catching common formats is enough; we are not
# building a full secret-scanner, just a basic safety net.
_SECRET_PATTERNS = [
    re.compile(r'[A-Za-z0-9+/]{40,}={0,2}'),     # base64 blob (API key, token)
    re.compile(r'sk-[A-Za-z0-9]{20,}'),            # OpenAI / Groq key prefix
    re.compile(r'gsk_[A-Za-z0-9]{20,}'),           # Groq key
    re.compile(r'eyJ[A-Za-z0-9._-]{30,}'),         # JWT (starts with eyJ)
    re.compile(r'-----BEGIN .{1,30} KEY-----'),     # PEM key header
    re.compile(r'(?i)password\s*[:=]\s*\S+'),       # "password: abc123"
    re.compile(r'(?i)secret\s*[:=]\s*\S{8,}'),     # "secret = xyzxyz"
    re.compile(r'(?i)token\s*[:=]\s*[A-Za-z0-9._-]{20,}'),  # token = ...
]


# ── Data types ─────────────────────────────────────────────────

@dataclass
class MemoryRow:
    id:         str
    key:        str
    value:      str
    source:     str
    created_at: str
    updated_at: str


@dataclass
class MemoryResult:
    ok:      bool
    message: str          # human-readable Hinglish message for the user
    data:    Optional[dict] = None


# ── Input validation ───────────────────────────────────────────

def _normalise_key(raw: str) -> tuple[bool, str, str]:
    """
    Normalise and validate a memory key.

    Returns (ok, normalised_key, error_message).
    Normalisation: strip, lowercase, replace spaces/hyphens with underscore.
    """
    raw = raw.strip()
    if not raw:
        return False, "", "Memory key must not be empty."

    normalised = raw.lower()
    normalised = re.sub(r'[\s\-]+', '_', normalised)   # spaces/hyphens → underscore
    normalised = re.sub(r'[^a-z0-9_]', '', normalised)  # strip anything else
    normalised = normalised[:MAX_KEY_LEN]

    if not normalised:
        return False, "", f"Memory key '{raw}' contains no valid characters."
    if not KEY_PATTERN.match(normalised):
        return False, "", f"Memory key '{normalised}' is invalid (use letters, digits, _ only)."

    return True, normalised, ""


def _validate_value(value: str) -> tuple[bool, str]:
    """
    Validate a memory value.

    Returns (ok, error_message).
    Rejects secrets. Caps length.
    """
    value = value.strip()
    if not value:
        return False, "Memory value must not be empty."

    if len(value) > MAX_VALUE_LEN:
        return False, f"Memory value too long ({len(value)} chars, max {MAX_VALUE_LEN})."

    for pattern in _SECRET_PATTERNS:
        if pattern.search(value):
            return False, (
                "Yeh value secret ya private key jaisi lag rahi hai. "
                "Nova sensitive information store nahi karta."
            )

    return True, ""


# ── DB helpers ─────────────────────────────────────────────────

def save_memory(
    user_id: str,
    key: str,
    value: str,
    source: str = "agent",
) -> MemoryResult:
    """
    Upsert a (key, value) fact for the authenticated user.

    If the key already exists for this user, its value is updated.
    If not, a new row is created.

    user_id must come from the verified JWT sub — never from request body.
    source: 'agent' | 'explicit' | 'api'
    """
    if not user_id:
        return MemoryResult(ok=False, message="User ID required to save memory.")

    ok, norm_key, key_err = _normalise_key(key)
    if not ok:
        return MemoryResult(ok=False, message=key_err)

    ok, val_err = _validate_value(value)
    if not ok:
        return MemoryResult(ok=False, message=val_err)

    if source not in ("agent", "explicit", "api"):
        source = "agent"

    db = get_db()
    if db is None:
        logger.warning("save_memory: Supabase unavailable, memory not saved")
        return MemoryResult(
            ok=False,
            message="Database unavailable. Memory is not saved right now."
        )

    try:
        db.table("user_memories").upsert(
            {
                "user_id": user_id,
                "key":     norm_key,
                "value":   value.strip(),
                "source":  source,
            },
            on_conflict="user_id,key",
        ).execute()
        logger.info(
            "Memory saved: user=%s key=%s source=%s",
            user_id[:8], norm_key, source,
        )
        return MemoryResult(
            ok=True,
            message=f"Yaad kar liya: '{norm_key}' = '{value.strip()[:60]}'.",
            data={"key": norm_key, "value": value.strip()},
        )
    except Exception as e:
        logger.error("save_memory error: %s", e)
        return MemoryResult(ok=False, message="Memory save karne mein error aaya. Dobara try karo.")


def recall_memories(
    user_id: str,
    key: Optional[str] = None,
) -> list[MemoryRow]:
    """
    Return memories for the authenticated user.

    If key is given: return the single matching memory (or empty list).
    If key is None:  return all memories, newest-updated first, up to MAX_MEMORIES.

    user_id must come from the verified JWT sub.
    """
    if not user_id:
        logger.warning("recall_memories: no user_id")
        return []

    db = get_db()
    if db is None:
        return []

    try:
        query = (
            db.table("user_memories")
            .select("id, key, value, source, created_at, updated_at")
            .eq("user_id", user_id)
        )
        if key is not None:
            ok, norm_key, _ = _normalise_key(key)
            if ok:
                query = query.eq("key", norm_key)
            else:
                return []
        else:
            query = query.order("updated_at", desc=True).limit(MAX_MEMORIES)

        resp = query.execute()
        rows = resp.data or []
        return [
            MemoryRow(
                id=r["id"],
                key=r["key"],
                value=r["value"],
                source=r["source"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]
    except Exception as e:
        logger.error("recall_memories error: %s", e)
        return []


def delete_memory(user_id: str, key: str) -> MemoryResult:
    """
    Delete the memory with the given key for the authenticated user.

    Returns ok=False (not an error) if the key does not exist.
    user_id must come from the verified JWT sub.
    """
    if not user_id:
        return MemoryResult(ok=False, message="User ID required to delete memory.")

    ok, norm_key, key_err = _normalise_key(key)
    if not ok:
        return MemoryResult(ok=False, message=key_err)

    db = get_db()
    if db is None:
        return MemoryResult(ok=False, message="Database unavailable. Memory delete nahi ho saka.")

    try:
        resp = (
            db.table("user_memories")
            .delete()
            .eq("user_id", user_id)
            .eq("key", norm_key)
            .execute()
        )
        deleted = len(resp.data) if resp.data else 0
        if deleted == 0:
            return MemoryResult(
                ok=False,
                message=f"'{norm_key}' yaad mein nahi tha. Kuch delete nahi hua.",
            )
        logger.info("Memory deleted: user=%s key=%s", user_id[:8], norm_key)
        return MemoryResult(
            ok=True,
            message=f"'{norm_key}' memory delete kar di.",
            data={"key": norm_key},
        )
    except Exception as e:
        logger.error("delete_memory error: %s", e)
        return MemoryResult(ok=False, message="Memory delete karne mein error aaya. Dobara try karo.")


def delete_all_memories(user_id: str) -> MemoryResult:
    """
    Delete ALL memories for the authenticated user.

    This is irreversible. The caller (DELETE_MEMORY with key='__all__')
    must have already shown the confirmation modal.
    user_id must come from the verified JWT sub.
    """
    if not user_id:
        return MemoryResult(ok=False, message="User ID required.")

    db = get_db()
    if db is None:
        return MemoryResult(ok=False, message="Database unavailable. Memory wipe nahi ho saka.")

    try:
        resp = (
            db.table("user_memories")
            .delete()
            .eq("user_id", user_id)
            .execute()
        )
        deleted = len(resp.data) if resp.data else 0
        logger.info("All memories deleted: user=%s count=%d", user_id[:8], deleted)
        return MemoryResult(
            ok=True,
            message=f"Saari memories delete kar di ({deleted} items). Fresh start! 🧹",
            data={"deleted_count": deleted},
        )
    except Exception as e:
        logger.error("delete_all_memories error: %s", e)
        return MemoryResult(ok=False, message="Memory wipe mein error aaya. Dobara try karo.")


def memory_count(user_id: str) -> int:
    """Return the number of stored memories for this user. Returns 0 on error."""
    if not user_id:
        return 0
    db = get_db()
    if db is None:
        return 0
    try:
        resp = (
            db.table("user_memories")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .execute()
        )
        return resp.count or 0
    except Exception:
        return 0


def format_memories_for_prompt(memories: list[MemoryRow]) -> str:
    """
    Serialise memories into a compact block for injection into the system
    prompt. Called by main.py when building the per-request context.

    Returns an empty string if the list is empty.
    Never includes id or source — only key/value pairs.
    """
    if not memories:
        return ""

    lines = ["USER MEMORY (facts Nova has been told to remember):"]
    for m in memories:
        # Truncate value in the prompt to 120 chars to keep context lean
        v = m.value if len(m.value) <= 120 else m.value[:117] + "…"
        lines.append(f"  • {m.key}: {v}")
    lines.append(
        "Use these facts naturally when relevant. "
        "Never recite the whole list unprompted."
    )
    return "\n".join(lines)
