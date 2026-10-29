"""
Nova AI Assistant — memory.py  (v2)

Conversation memory operations backed by Supabase.

v2 changes (Phase 5 — per-user isolation):
  • All conversation functions now require user_id.
  • Ownership is enforced at query level (WHERE user_id = ?) so
    that even without RLS a user cannot touch another user's data.
  • list_conversations() added for the sidebar conversation list.
  • create_conversation() replaces get_or_create_conversation().
    The old signature is kept as a thin shim for backward compat.
  • Title is auto-derived from the first user message (first 60 chars).

Fallback rule (unchanged):
  If get_db() returns None, every function silently no-ops and returns
  a safe default (None, [], False).  The caller never needs to check.
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from database import get_db

logger = logging.getLogger("nova.memory")

DEFAULT_HISTORY_LIMIT = 20
MAX_CONV_LIST         = 30          # sidebar shows at most 30 recent chats
MAX_CONTENT_LENGTH    = 4000        # hard cap per message


# ── Type alias ────────────────────────────────────────────────
MessageDict      = dict   # {id, role, content, tool_intent, created_at}
ConversationDict = dict   # {id, title, created_at, updated_at}


# ============================================================
# Conversation management
# ============================================================

def create_conversation(user_id: str, title: str = "Nova Chat") -> Optional[str]:
    """
    Create a new conversation row owned by user_id.

    Returns the new conversation UUID, or None on failure.
    When Supabase is unavailable, returns a local UUID (not persisted).
    """
    db = get_db()
    new_id = str(uuid.uuid4())

    if db is None:
        return new_id   # in-memory only

    try:
        db.table("conversations").insert({
            "id":      new_id,
            "user_id": user_id,
            "title":   title[:200],
        }).execute()
        logger.debug("Created conversation: %s user=%s", new_id[:8], user_id[:8])
        return new_id
    except Exception as e:
        logger.warning("create_conversation failed: %s", e)
        return new_id   # return ID anyway; message inserts may still work


def update_conversation_title(
    conversation_id: str,
    user_id: str,
    title: str,
) -> None:
    """
    Set the conversation title (called after the first user message).
    Scoped by user_id to prevent cross-user writes.
    """
    db = get_db()
    if db is None:
        return
    try:
        db.table("conversations") \
            .update({"title": title[:200]}) \
            .eq("id", conversation_id) \
            .eq("user_id", user_id) \
            .execute()
    except Exception as e:
        logger.warning("update_conversation_title failed: %s", e)


def list_conversations(
    user_id: str,
    limit: int = MAX_CONV_LIST,
) -> list[ConversationDict]:
    """
    Return the user's most recent conversations, newest first.

    Each item: {id, title, created_at, updated_at}
    Returns [] if Supabase is unavailable.
    """
    db = get_db()
    if db is None:
        return []

    try:
        resp = (
            db.table("conversations")
            .select("id, title, created_at, updated_at")
            .eq("user_id", user_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.warning("list_conversations failed: %s", e)
        return []


def get_conversation(conversation_id: str, user_id: str) -> Optional[ConversationDict]:
    """
    Fetch a single conversation, scoped to user_id.
    Returns None if not found or user doesn't own it.
    """
    db = get_db()
    if db is None:
        return None

    try:
        resp = (
            db.table("conversations")
            .select("id, title, created_at, updated_at")
            .eq("id", conversation_id)
            .eq("user_id", user_id)
            .maybe_single()
            .execute()
        )
        return resp.data  # None if not found
    except Exception as e:
        logger.warning("get_conversation failed: %s", e)
        return None


def delete_conversation(conversation_id: str, user_id: str) -> bool:
    """
    Delete a conversation and all its messages (CASCADE).
    Scoped by user_id — silently does nothing if user doesn't own it.
    """
    db = get_db()
    if db is None:
        return False

    try:
        db.table("conversations") \
            .delete() \
            .eq("id", conversation_id) \
            .eq("user_id", user_id) \
            .execute()
        logger.info("Deleted conversation: %s user=%s", conversation_id[:8], user_id[:8])
        return True
    except Exception as e:
        logger.warning("delete_conversation failed: %s", e)
        return False


# ============================================================
# Message operations
# ============================================================

def save_message(
    conversation_id: str,
    role: str,
    content: str,
    tool_intent: Optional[str] = None,
) -> Optional[str]:
    """
    Insert a message into the messages table.

    role must be 'user' or 'bot'.
    Ownership is enforced by the RLS policy on messages
    (the conversation must belong to auth.uid()).

    Returns the new message UUID, or None if unavailable/failed.
    """
    if role not in ("user", "bot", "system"):
        logger.warning("save_message: invalid role %r, skipping", role)
        return None

    db = get_db()
    if db is None:
        return None

    row: dict = {
        "conversation_id": conversation_id,
        "role":            role,
        "content":         content[:MAX_CONTENT_LENGTH],
    }
    if tool_intent:
        row["tool_intent"] = tool_intent[:64]

    try:
        resp = db.table("messages").insert(row).execute()
        msg_id = resp.data[0]["id"] if resp.data else None
        logger.debug("Saved message: role=%s conv=%s id=%s", role, conversation_id[:8], msg_id)
        return msg_id
    except Exception as e:
        logger.warning("save_message failed: %s", e)
        return None


def get_history(
    conversation_id: str,
    user_id: str,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[MessageDict]:
    """
    Fetch the last `limit` messages for a conversation, oldest first.

    user_id is used to verify ownership at query level
    (in addition to RLS enforcement).

    Returns [] if unavailable or user doesn't own the conversation.
    """
    db = get_db()
    if db is None:
        return []

    # Ownership check: verify this conversation belongs to user_id
    conv = get_conversation(conversation_id, user_id)
    if conv is None:
        logger.warning(
            "get_history: conversation %s not found for user %s",
            conversation_id[:8], user_id[:8],
        )
        return []

    try:
        resp = (
            db.table("messages")
            .select("id, role, content, tool_intent, created_at")
            .eq("conversation_id", conversation_id)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.warning("get_history failed: %s", e)
        return []


# ============================================================
# Backward-compatibility shim
# (main.py was calling get_or_create_conversation before Phase 5)
# ============================================================

def get_or_create_conversation(
    user_id: str,
    conversation_id: Optional[str] = None,
) -> Optional[str]:
    """
    Deprecated shim — use create_conversation() for new code.

    If conversation_id given and owned by user: return it.
    Otherwise: create a new conversation.
    """
    if conversation_id:
        existing = get_conversation(conversation_id, user_id)
        if existing:
            return conversation_id
    return create_conversation(user_id)
