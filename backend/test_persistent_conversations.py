"""
Nova AI Assistant — test_persistent_conversations.py

Offline tests for persistent conversation features:
  1.  History injected into agent loop when conversation_id supplied
  2.  History NOT injected for anonymous users (no user_id)
  3.  History NOT injected for new conversations (no conversation_id)
  4.  History capped at AGENT_HISTORY_TURNS * 2 messages
  5.  History failure is non-fatal — loop still runs
  6.  Correct role mapping: "bot" → "assistant" for Groq
  7.  Empty/blank history messages are skipped
  8.  Backend endpoints: GET /conversations, POST /conversations,
      GET /conversations/{id}/messages, DELETE /conversations/{id}
  9.  /chat response always includes conversation_id
  10. conversation_id sent by frontend is forwarded to run_agent_loop
  11. No history for anonymous users in /chat
  12. get_conversation import present in main (for ownership checks)
  13. AGENT_HISTORY_TURNS env var respected

Run from the backend/ directory:
    python test_persistent_conversations.py
"""

import sys
import os
import json
import types
import logging
from unittest.mock import MagicMock, patch, call

logging.disable(logging.CRITICAL)

import main
import memory

PASS = "[PASS]"
FAIL = "[FAIL]"
errors = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global errors
    if ok:
        print(f"  {PASS} {label}")
    else:
        errors += 1
        print(f"  {FAIL} {label}" + (f"  -- {detail}" if detail else ""))


# ============================================================================
# Helpers
# ============================================================================

class _FakeToolCall:
    def __init__(self, name, arguments, call_id="cid_1"):
        self.id = call_id
        self.function = types.SimpleNamespace(name=name, arguments=json.dumps(arguments))


def _make_msg(text=None, tool_calls=None):
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = tool_calls or []
    return msg


def _stub_groq(response_text="Haan, yaad hai!"):
    """Patch _groq_completion_with_fallback to return a single plain-text reply."""
    def _fake(client, messages, tools):
        return _make_msg(response_text), None
    return patch.object(main, "_groq_completion_with_fallback", side_effect=_fake)


# ============================================================================
# 1. History injected when conversation_id and user_id are both present
# ============================================================================
print("\n=== 1. History injected into agent loop ===")

fake_history = [
    {"id": "a", "role": "user",  "content": "Mera naam Arjun hai.", "tool_intent": None, "created_at": "2026-01-01"},
    {"id": "b", "role": "bot",   "content": "Namaste Arjun!",       "tool_intent": None, "created_at": "2026-01-01"},
]

captured_messages = []

def _capture_groq(client, messages, tools):
    captured_messages.clear()
    captured_messages.extend(messages)
    return _make_msg("Haan Arjun, kya poochhna tha?"), None

with patch.object(main, "_groq_completion_with_fallback", side_effect=_capture_groq), \
     patch("main.get_history", return_value=fake_history):

    result = main.run_agent_loop(
        client=MagicMock(),
        user_message="Mera naam kya hai?",
        user_id="user-123",
        conversation_id="conv-abc",
    )

# messages[0] = system, messages[1] = user (history), messages[2] = assistant (history),
# messages[3] = current user message
check("history injected into messages", len(captured_messages) >= 4,
      f"got {len(captured_messages)} messages")
check("system prompt is first", captured_messages[0]["role"] == "system")

# find history messages
hist_roles = [m["role"] for m in captured_messages[1:-1]]
check("history user turn mapped to 'user'", "user" in hist_roles)
check("history bot turn mapped to 'assistant'", "assistant" in hist_roles)
check("history content preserved",
      any("Arjun" in m.get("content","") for m in captured_messages))
check("current message is last", captured_messages[-1]["content"] == "Mera naam kya hai?")
check("result type is message", result.get("type") == "message")

# ============================================================================
# 2. No history for anonymous users
# ============================================================================
print("\n=== 2. No history for anonymous users ===")

captured_anon = []

def _capture_anon(client, messages, tools):
    captured_anon.clear()
    captured_anon.extend(messages)
    return _make_msg("Hello!"), None

with patch.object(main, "_groq_completion_with_fallback", side_effect=_capture_anon), \
     patch("main.get_history", return_value=fake_history) as mock_gh:

    main.run_agent_loop(
        client=MagicMock(),
        user_message="Hi",
        user_id=None,             # anonymous
        conversation_id="conv-abc",
    )

check("get_history NOT called for anonymous user", not mock_gh.called)
check("only system + user in messages for anon", len(captured_anon) == 2,
      f"got {len(captured_anon)}")

# ============================================================================
# 3. No history for new conversations (no conversation_id)
# ============================================================================
print("\n=== 3. No history for new conversations ===")

captured_new = []

def _capture_new(client, messages, tools):
    captured_new.clear()
    captured_new.extend(messages)
    return _make_msg("Kya haal hai?"), None

with patch.object(main, "_groq_completion_with_fallback", side_effect=_capture_new), \
     patch("main.get_history", return_value=fake_history) as mock_gh2:

    main.run_agent_loop(
        client=MagicMock(),
        user_message="Hi",
        user_id="user-123",
        conversation_id=None,    # new conversation
    )

check("get_history NOT called for new conversation", not mock_gh2.called)
check("only system + user in messages for new conv", len(captured_new) == 2,
      f"got {len(captured_new)}")

# ============================================================================
# 4. History capped by AGENT_HISTORY_TURNS
# ============================================================================
print("\n=== 4. History capped at AGENT_HISTORY_TURNS ===")

# Build 30 fake history messages (15 pairs)
long_history = []
for i in range(15):
    long_history.append({"id": f"u{i}", "role": "user",  "content": f"msg {i}", "tool_intent": None, "created_at": "2026-01-01"})
    long_history.append({"id": f"b{i}", "role": "bot",   "content": f"reply {i}", "tool_intent": None, "created_at": "2026-01-01"})

captured_capped = []

def _capture_capped(client, messages, tools):
    captured_capped.clear()
    captured_capped.extend(messages)
    return _make_msg("OK"), None

# Patch get_history to return all 30; AGENT_HISTORY_TURNS=5 (10 messages)
with patch.dict(os.environ, {"AGENT_HISTORY_TURNS": "5"}), \
     patch.object(main, "_groq_completion_with_fallback", side_effect=_capture_capped), \
     patch("main.get_history", return_value=long_history[:10]) as mock_capped:

    main.run_agent_loop(
        client=MagicMock(),
        user_message="Hi",
        user_id="user-123",
        conversation_id="conv-abc",
    )

# get_history was called with limit = AGENT_HISTORY_TURNS * 2 = 10
check("get_history called with limit=10",
      mock_capped.called and mock_capped.call_args[1].get("limit") == 10
      or (mock_capped.called and len(mock_capped.call_args[0]) >= 3
          and mock_capped.call_args[0][2] == 10),
      str(mock_capped.call_args))

# ============================================================================
# 5. History load failure is non-fatal
# ============================================================================
print("\n=== 5. History failure is non-fatal ===")

with _stub_groq("Theek hun!"), \
     patch("main.get_history", side_effect=Exception("DB down")):

    result = main.run_agent_loop(
        client=MagicMock(),
        user_message="Hello",
        user_id="user-123",
        conversation_id="conv-abc",
    )

check("loop succeeds despite history error", result.get("type") == "message")
check("response present", bool(result.get("response")))

# ============================================================================
# 6. Bot role mapping: "bot" → "assistant"
# ============================================================================
print("\n=== 6. Role mapping bot → assistant ===")

mixed_history = [
    {"id": "a", "role": "user", "content": "Question", "tool_intent": None, "created_at": "2026-01-01"},
    {"id": "b", "role": "bot",  "content": "Answer",   "tool_intent": None, "created_at": "2026-01-01"},
]
captured_roles = []

def _capture_roles(client, messages, tools):
    captured_roles.clear()
    captured_roles.extend(messages)
    return _make_msg("OK"), None

with patch.object(main, "_groq_completion_with_fallback", side_effect=_capture_roles), \
     patch("main.get_history", return_value=mixed_history):

    main.run_agent_loop(
        client=MagicMock(), user_message="Hi",
        user_id="u1", conversation_id="c1",
    )

hist_mapped = [m["role"] for m in captured_roles[1:-1]]
check("'bot' mapped to 'assistant'", "assistant" in hist_mapped,
      str(hist_mapped))
check("'user' stays 'user'", "user" in hist_mapped)
check("no 'bot' role in messages",  "bot" not in hist_mapped,
      str(hist_mapped))

# ============================================================================
# 7. Empty/blank history messages are skipped
# ============================================================================
print("\n=== 7. Empty messages skipped ===")

history_with_blanks = [
    {"id": "a", "role": "user", "content": "",      "tool_intent": None, "created_at": "2026-01-01"},  # blank
    {"id": "b", "role": "bot",  "content": "  ",    "tool_intent": None, "created_at": "2026-01-01"},  # whitespace
    {"id": "c", "role": "user", "content": "Valid", "tool_intent": None, "created_at": "2026-01-01"},
]
captured_blanks = []

def _capture_blanks(client, messages, tools):
    captured_blanks.clear()
    captured_blanks.extend(messages)
    return _make_msg("OK"), None

with patch.object(main, "_groq_completion_with_fallback", side_effect=_capture_blanks), \
     patch("main.get_history", return_value=history_with_blanks):

    main.run_agent_loop(
        client=MagicMock(), user_message="Hi",
        user_id="u1", conversation_id="c1",
    )

hist_contents = [m["content"] for m in captured_blanks[1:-1]]
check("blank messages not in history",
      "" not in hist_contents and "  " not in hist_contents,
      str(hist_contents))
check("'Valid' message is included",
      "Valid" in hist_contents, str(hist_contents))

# ============================================================================
# 8. Backend: /conversations routes exist and are registered
# ============================================================================
print("\n=== 8. Backend routes registered ===")

routes_list = [(r.path, list(r.methods)) for r in main.app.routes if hasattr(r, "path")]

def _has_route(path, method):
    return any(p == path and method in (ms or []) for p, ms in routes_list)

check("GET /conversations registered",   _has_route("/conversations", "GET"))
check("POST /conversations registered",  _has_route("/conversations", "POST"))
check("GET /conversations/{conv_id}/messages registered",
      _has_route("/conversations/{conv_id}/messages", "GET"))
check("DELETE /conversations/{conv_id} registered",
      _has_route("/conversations/{conv_id}", "DELETE"))

# ============================================================================
# 9. /chat response always includes conversation_id
# ============================================================================
print("\n=== 9. /chat always returns conversation_id ===")

import inspect
chat_src = inspect.getsource(main.chat)
check("result['conversation_id'] set in /chat", 'result["conversation_id"]' in chat_src
      or "result['conversation_id']" in chat_src)

# ============================================================================
# 10. conversation_id forwarded to run_agent_loop
# ============================================================================
print("\n=== 10. conversation_id forwarded to agent loop ===")

loop_src = inspect.getsource(main.run_agent_loop)
check("run_agent_loop accepts conversation_id param",
      "conversation_id" in loop_src)
check("get_history called with conversation_id",
      "get_history" in loop_src and "conversation_id" in loop_src)

# ============================================================================
# 11. No history injection for anonymous users in run_agent_loop source
# ============================================================================
print("\n=== 11. Anonymous users get no history (source audit) ===")

check("guard: user_id AND conversation_id required for history",
      "if user_id and conversation_id" in loop_src
      or "user_id and conversation_id" in loop_src)

# ============================================================================
# 12. get_conversation imported in main
# ============================================================================
print("\n=== 12. get_conversation imported in main ===")

check("get_conversation in main module", hasattr(main, "get_conversation")
      or "get_conversation" in inspect.getsource(main))

# ============================================================================
# 13. AGENT_HISTORY_TURNS env var
# ============================================================================
print("\n=== 13. AGENT_HISTORY_TURNS respected ===")

check("AGENT_HISTORY_TURNS read from env in loop source",
      "AGENT_HISTORY_TURNS" in loop_src)

# ============================================================================
# Summary
# ============================================================================
print(f"\n{'=' * 55}")
if errors == 0:
    print("ALL PERSISTENT CONVERSATION TESTS PASSED")
else:
    print(f"{errors} TEST(S) FAILED")
sys.exit(1 if errors > 0 else 0)
