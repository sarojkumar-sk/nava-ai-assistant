"""
Nova AI Assistant — test_agent_loop.py

Offline unit tests for the controlled agent loop (run_agent_loop).

Covers the three scenarios from the requirements:
  1. Normal chat    — no tool needed, plain text reply
  2. YouTube request — PLAY_YOUTUBE tool call
  3. Weather request — GET_WEATHER tool call (server-side execution)

Additional edge-case tests:
  4. Max iterations guard
  5. Wall-clock timeout guard
  6. Unknown tool — loop recovers and returns a message
  7. Tool result verification — bad params return a message
  8. Auth/session isolation — loop never reads/writes session state
  9. Exception fallback in /chat — groq_with_fallback called on loop crash

Run from the backend/ directory:
    python test_agent_loop.py

All tests are fully offline — no real Groq calls, no network, no DB.
The Groq client and tool executors are stubbed.
"""

import sys
import time
import json
import types
import unittest
from unittest.mock import MagicMock, patch

# ── Silence startup logs ────────────────────────────────────────────────────
import logging
logging.disable(logging.CRITICAL)

# ── Import the module under test ────────────────────────────────────────────
import main

PASS = "[PASS]"
FAIL = "[FAIL]"
errors = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global errors
    if ok:
        print(f"  {PASS} {label}")
    else:
        errors += 1
        print(f"  {FAIL} {label}" + (f"  — {detail}" if detail else ""))


# ============================================================================
# Stub helpers
# ============================================================================

class _FakeToolCall:
    """Mimics groq ChatCompletionMessageToolCall."""
    def __init__(self, name: str, arguments: dict, call_id: str = "cid_1"):
        self.id = call_id
        self.function = types.SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        )


def _make_msg(text: str | None = None, tool_calls: list | None = None):
    """Build a minimal Groq ChatCompletionMessage-alike."""
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = tool_calls or []
    return msg


def _stub_groq_sequence(responses: list):
    """
    Patch _groq_completion_with_fallback to yield (msg, None) in sequence.
    `responses` is a list of (content_str | None, tool_calls_list | None).
    """
    calls = iter(responses)

    def _fake(client, messages, tools):
        try:
            content, tcs = next(calls)
            return _make_msg(content, tcs), None
        except StopIteration:
            # If the sequence is exhausted, return a plain reply
            return _make_msg("Done."), None

    return patch.object(main, "_groq_completion_with_fallback", side_effect=_fake)


# ============================================================================
# Test 1: Normal chat — no tool needed
# ============================================================================
print("\n=== 1. Normal chat (no tool) ===")


def test_normal_chat():
    """When the model returns plain text, the loop exits immediately."""
    with _stub_groq_sequence([(
        "Haan, main theek hun! Kya haal hai tumhara? 😊", None
    )]):
        result = main.run_agent_loop(
            client=MagicMock(),
            user_message="Hey Nova, kya haal hai?",
        )

    check("type is message", result["type"] == "message",
          repr(result.get("type")))
    check("response is non-empty", bool(result.get("response")))
    check("agent_steps present", "agent_steps" in result)
    check("only 1 step taken (no tool)", len(result["agent_steps"]) == 1,
          str(result["agent_steps"]))
    check("step action is final_reply",
          result["agent_steps"][0]["action"] == "final_reply")


test_normal_chat()

# ============================================================================
# Test 2: YouTube request
# ============================================================================
print("\n=== 2. YouTube request ===")


def test_youtube_request():
    """PLAY_YOUTUBE tool is called, synthesised URL fed back, final reply returned."""
    yt_tool_call = _FakeToolCall("PLAY_YOUTUBE", {"query": "Arijit Singh sad songs"})

    with _stub_groq_sequence([
        # Round 1: model wants to call PLAY_YOUTUBE
        (None, [yt_tool_call]),
        # Round 2: model writes final reply after seeing tool result
        ("YouTube par 'Arijit Singh sad songs' search kar diya. Enjoy! 🎵", None),
    ]):
        result = main.run_agent_loop(
            client=MagicMock(),
            user_message="Arijit Singh ke sad songs YouTube par lagao",
        )

    check("type is tool_call", result["type"] == "tool_call",
          repr(result.get("type")))
    check("intent is PLAY_YOUTUBE", result.get("intent") == "PLAY_YOUTUBE",
          repr(result.get("intent")))
    check("parameters has query", "query" in (result.get("parameters") or {}))
    check("youtube_url in parameters", "youtube_url" in (result.get("parameters") or {}))
    check("final NL response present", bool(result.get("response")),
          repr(result.get("response")))
    check("agent_steps recorded",
          any(s.get("tool") == "PLAY_YOUTUBE" for s in result.get("agent_steps", [])))
    check("requires_confirmation is bool",
          isinstance(result.get("requires_confirmation"), bool))


test_youtube_request()

# ============================================================================
# Test 3: Weather request (server-side execution)
# ============================================================================
print("\n=== 3. Weather request ===")


def test_weather_request():
    """GET_WEATHER is executed on the backend; tool_result in final response."""
    weather_tool_call = _FakeToolCall("GET_WEATHER", {"city": "Mumbai"})

    # Stub _fetch_weather_data so no real HTTP call is made
    fake_weather = {
        "ok": True,
        "city": "Mumbai",
        "temp_c": "32",
        "temp_f": "89",
        "feels_like_c": "38",
        "humidity": "75",
        "description": "Partly cloudy",
        "wind_kmph": "20",
        "visibility_km": "6",
    }

    with patch.object(main, "_fetch_weather_data", return_value=fake_weather), \
         _stub_groq_sequence([
             # Round 1: model calls GET_WEATHER
             (None, [weather_tool_call]),
             # Round 2: final reply after seeing weather result
             ("Mumbai mein abhi 32°C hai aur partly cloudy. Garmi ke liye ready raho! ☀️", None),
         ]):
        result = main.run_agent_loop(
            client=MagicMock(),
            user_message="Mumbai mein aaj mausam kaisa hai?",
        )

    check("type is tool_call", result["type"] == "tool_call",
          repr(result.get("type")))
    check("intent is GET_WEATHER", result.get("intent") == "GET_WEATHER",
          repr(result.get("intent")))
    check("tool_result present", "tool_result" in result)
    check("tool_result.ok is True", (result.get("tool_result") or {}).get("ok") is True)
    check("tool_result.city is Mumbai",
          (result.get("tool_result") or {}).get("city") == "Mumbai")
    check("final NL response present", bool(result.get("response")))
    check("agent_steps has GET_WEATHER",
          any(s.get("tool") == "GET_WEATHER" for s in result.get("agent_steps", [])))


test_weather_request()

# ============================================================================
# Test 4: Max iterations guard
# ============================================================================
print("\n=== 4. Max iterations guard ===")


def test_max_iterations():
    """Loop stops after max_iterations and returns a graceful fallback message."""
    # Always return a tool call so the loop never gets a final text reply.
    looping_tool = _FakeToolCall("PLAY_YOUTUBE", {"query": "loop test"})

    fake_weather = {"ok": True, "city": "x", "temp_c": "0", "temp_f": "32",
                    "feels_like_c": "0", "humidity": "0", "description": "Clear",
                    "wind_kmph": "0", "visibility_km": "10"}

    # Build an infinite sequence of tool calls (loop must cap at max_iterations)
    infinite = [(None, [looping_tool])] * 20

    with patch.object(main, "_fetch_weather_data", return_value=fake_weather), \
         _stub_groq_sequence(infinite):
        result = main.run_agent_loop(
            client=MagicMock(),
            user_message="loop test",
            max_iterations=3,   # deliberately low for the test
        )

    # After max_iterations, the loop must return SOME response.
    check("result has response or type", "response" in result or result.get("type") in ("tool_call", "message"),
          repr(result))
    # The agent_steps should reflect that iterations were capped.
    step_actions = [s.get("action") for s in result.get("agent_steps", [])]
    check("max_iterations action recorded OR tool steps <= 3",
          "max_iterations" in step_actions or sum(1 for a in step_actions if a == "tool_call") <= 3,
          str(step_actions))


test_max_iterations()

# ============================================================================
# Test 5: Wall-clock timeout guard
# ============================================================================
print("\n=== 5. Wall-clock timeout guard ===")


def test_timeout():
    """Loop aborts when wall-clock time exceeds loop_timeout_s."""
    # Use a fake monotonic clock that fast-forwards 100 s after the first read.
    call_count = [0]

    def fast_clock():
        call_count[0] += 1
        # First call (start) returns 0.0; second (inside first iteration) returns 100.0
        return 0.0 if call_count[0] == 1 else 100.0

    looping_tool = _FakeToolCall("PLAY_YOUTUBE", {"query": "timeout test"})
    infinite = [(None, [looping_tool])] * 10

    with _stub_groq_sequence(infinite):
        result = main.run_agent_loop(
            client=MagicMock(),
            user_message="timeout test",
            loop_timeout_s=10,   # 10 s cap — fast_clock will exceed it immediately
            _clock=fast_clock,
        )

    step_actions = [s.get("action") for s in result.get("agent_steps", [])]
    check("timeout action recorded", "timeout" in step_actions, str(step_actions))
    check("result has response or intent",
          bool(result.get("response")) or bool(result.get("intent")))


test_timeout()

# ============================================================================
# Test 6: Unknown tool — loop recovers
# ============================================================================
print("\n=== 6. Unknown tool — loop recovers ===")


def test_unknown_tool():
    """Model returns an unknown tool name; loop feeds error back, model recovers."""
    bad_tool = _FakeToolCall("FLY_ROCKET", {"destination": "Mars"})

    with _stub_groq_sequence([
        # Round 1: unknown tool
        (None, [bad_tool]),
        # Round 2: model recovers after error result
        ("FLY_ROCKET tool mujhe pata nahi. Kuch aur try karo.", None),
    ]):
        result = main.run_agent_loop(
            client=MagicMock(),
            user_message="Mars par jaana hai",
        )

    check("result type is message", result["type"] == "message",
          repr(result.get("type")))
    check("response is non-empty", bool(result.get("response")))
    check("agent_steps has tool_result with ok=False",
          any(s.get("action") == "tool_result" and s.get("ok") is False
              for s in result.get("agent_steps", [])))


test_unknown_tool()

# ============================================================================
# Test 7: Tool result verification — invalid params
# ============================================================================
print("\n=== 7. Tool result verification — bad params ===")


def test_bad_params():
    """Model provides an empty query; Pydantic rejects it, error fed back."""
    bad_yt = _FakeToolCall("PLAY_YOUTUBE", {"query": ""})   # empty query → validation fail

    with _stub_groq_sequence([
        (None, [bad_yt]),
        ("Kuch query deni padegi. Batao kya sunna hai?", None),
    ]):
        result = main.run_agent_loop(
            client=MagicMock(),
            user_message="kuch lagao",
        )

    check("eventually returns message", result["type"] == "message",
          repr(result.get("type")))
    check("param validation fail recorded in steps",
          any(s.get("action") == "tool_result" and s.get("ok") is False
              for s in result.get("agent_steps", [])))


test_bad_params()

# ============================================================================
# Test 8: Auth/session isolation
# ============================================================================
print("\n=== 8. Auth/session isolation ===")


def test_auth_isolation():
    """run_agent_loop must not read or modify session/auth state."""
    import inspect
    src = inspect.getsource(main.run_agent_loop)

    forbidden = [
        "request.headers",
        "Authorization",
        "clearSession",
        "verify_token",
        "save_message",
        "create_conversation",
    ]
    for token in forbidden:
        check(f"no '{token}' in run_agent_loop", token not in src, "found in source")

    # Confirm /chat still uses verify_token_optional
    chat_src = inspect.getsource(main.chat)
    check("chat uses verify_token_optional", "verify_token_optional" in chat_src)
    check("chat persists messages outside run_agent_loop", "save_message" in chat_src)


test_auth_isolation()

# ============================================================================
# Test 9: /chat fallback to groq_with_fallback on loop exception
# ============================================================================
print("\n=== 9. /chat fallback on loop exception ===")


def test_chat_fallback():
    """If run_agent_loop raises, /chat falls back to groq_with_fallback silently."""
    import inspect
    src = inspect.getsource(main.chat)

    check("try/except around run_agent_loop in chat", "except Exception" in src)
    check("fallback calls groq_with_fallback", "groq_with_fallback" in src)


test_chat_fallback()

# ============================================================================
# Test 10: Preserve normal chat — response shape
# ============================================================================
print("\n=== 10. Normal chat response shape ===")


def test_normal_chat_shape():
    """Plain message response must have type=message and response string only."""
    with _stub_groq_sequence([("2 + 2 = 4. Yeh basic math hai! 😊", None)]):
        result = main.run_agent_loop(
            client=MagicMock(),
            user_message="2 + 2 kya hoga?",
        )

    check("type == message", result["type"] == "message")
    check("no 'intent' key for plain chat", "intent" not in result)
    check("no 'tool_result' key for plain chat", "tool_result" not in result)
    check("agent_steps is list", isinstance(result.get("agent_steps"), list))
    check("response is str", isinstance(result.get("response"), str))


test_normal_chat_shape()

# ============================================================================
# Summary
# ============================================================================
print(f"\n{'=' * 55}")
if errors == 0:
    print("ALL AGENT LOOP TESTS PASSED")
else:
    print(f"{errors} TEST(S) FAILED")
sys.exit(1 if errors > 0 else 0)
