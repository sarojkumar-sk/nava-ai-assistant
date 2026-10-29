"""
Nova AI Assistant — test_confirmation_flow.py

Tests for the high-risk / low-risk confirmation gate.

Covers:
  1.  Risk classification — requires_confirmation values per tool
  2.  DELETE_MEMORY tool exists, is HIGH risk, available in chat
  3.  DELETE_MEMORY param validation (confirm=True required)
  4.  SEND_MESSAGE alias exists in android_tools, is HIGH risk
  5.  CALL_CONTACT is HIGH risk
  6.  SEND_SMS is HIGH risk
  7.  Low-risk tools do NOT require confirmation
  8.  DELETE_MEMORY schema offered to LLM in groq_tool_schemas
  9.  _execute_tool_in_loop handles DELETE_MEMORY correctly
  10. ALL_TOOL_NAMES contains both SEND_SMS and SEND_MESSAGE
  11. android_tools: SEND_MESSAGE and SEND_SMS share same param model
  12. Groq schema for DELETE_MEMORY marks confirm as required
  13. groq_android_tools_schema includes SEND_MESSAGE
  14. No real phone call claimed — CALL_CONTACT is ACTION_DIAL not ACTION_CALL by default

Run from the backend/ directory:
    python test_confirmation_flow.py
"""

import sys
import logging

logging.disable(logging.CRITICAL)

import tool_registry
import android_tools
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
        print(f"  {FAIL} {label}" + (f"  -- {detail}" if detail else ""))


# ============================================================================
# 1. Risk classification
# ============================================================================
print("\n=== 1. Risk classification: requires_confirmation per tool ===")

HIGH_RISK_EXPECTED = {"CALL_CONTACT", "SEND_SMS", "SEND_MESSAGE", "DELETE_MEMORY", "OPEN_WEBSITE"}
LOW_RISK_EXPECTED  = {"SEARCH_YOUTUBE", "PLAY_YOUTUBE", "GET_WEATHER", "SET_ALARM", "GET_CONTACTS"}

# Browser tools
for name, tool in tool_registry.TOOL_REGISTRY.items():
    if name in ("ANALYZE_IMAGE",):
        continue   # not offered in chat, skip
    expected_high = name in HIGH_RISK_EXPECTED
    check(
        f"{name}.requires_confirmation == {expected_high}",
        tool.requires_confirmation == expected_high,
        f"got {tool.requires_confirmation}",
    )

# Android tools
for name, tool in android_tools.ANDROID_TOOL_REGISTRY.items():
    expected_high = name in HIGH_RISK_EXPECTED
    check(
        f"[Android] {name}.requires_confirmation == {expected_high}",
        tool.requires_confirmation == expected_high,
        f"got {tool.requires_confirmation}",
    )


# ============================================================================
# 2. DELETE_MEMORY tool registered
# ============================================================================
print("\n=== 2. DELETE_MEMORY in tool_registry ===")

dm = tool_registry.get_tool("DELETE_MEMORY")
check("DELETE_MEMORY exists", dm is not None)
check("DELETE_MEMORY risk is HIGH", dm is not None and dm.risk == tool_registry.ToolRiskLevel.HIGH)
check("DELETE_MEMORY available_in_chat", dm is not None and dm.available_in_chat is True)
check("DELETE_MEMORY in CHAT_TOOL_NAMES", "DELETE_MEMORY" in tool_registry.CHAT_TOOL_NAMES)
check("DELETE_MEMORY requires_confirmation", dm is not None and dm.requires_confirmation is True)


# ============================================================================
# 3. DELETE_MEMORY param validation
# ============================================================================
print("\n=== 3. DELETE_MEMORY param validation ===")

ok, err, model = dm.validate_parameters({"confirm": True})
check("confirm=True -> ok=True", ok, err)

ok, err, model = dm.validate_parameters({"confirm": False})
check("confirm=False -> ok=False", not ok, "should require True")

ok, err, model = dm.validate_parameters({})
check("missing confirm -> uses default (True) -> ok=True", ok, err)


# ============================================================================
# 4. SEND_MESSAGE alias in android_tools
# ============================================================================
print("\n=== 4. SEND_MESSAGE alias in android_tools ===")

check("SEND_MESSAGE in ANDROID_TOOL_NAMES", "SEND_MESSAGE" in android_tools.ANDROID_TOOL_NAMES)
check("SEND_SMS in ANDROID_TOOL_NAMES",     "SEND_SMS"     in android_tools.ANDROID_TOOL_NAMES)

sm = android_tools.get_android_tool("SEND_MESSAGE")
ss = android_tools.get_android_tool("SEND_SMS")
check("SEND_MESSAGE tool exists",           sm is not None)
check("SEND_SMS tool exists",               ss is not None)
check("SEND_MESSAGE requires_confirmation", sm is not None and sm.requires_confirmation is True)
check("SEND_SMS requires_confirmation",     ss is not None and ss.requires_confirmation is True)


# ============================================================================
# 5. CALL_CONTACT risk
# ============================================================================
print("\n=== 5. CALL_CONTACT risk ===")

cc = android_tools.get_android_tool("CALL_CONTACT")
check("CALL_CONTACT requires_confirmation", cc is not None and cc.requires_confirmation is True)
check("CALL_CONTACT in ANDROID_TOOL_NAMES", "CALL_CONTACT" in android_tools.ANDROID_TOOL_NAMES)


# ============================================================================
# 6. Low-risk tools — SET_ALARM, GET_CONTACTS, PLAY_YOUTUBE, etc.
# ============================================================================
print("\n=== 6. Low-risk tools do NOT require confirmation ===")

for name in ("SET_ALARM", "GET_CONTACTS"):
    t = android_tools.get_android_tool(name)
    check(f"[Android] {name}.requires_confirmation == False",
          t is not None and t.requires_confirmation is False,
          f"got {t.requires_confirmation if t else 'None'}")

for name in ("PLAY_YOUTUBE", "SEARCH_YOUTUBE", "GET_WEATHER"):
    t = tool_registry.get_tool(name)
    check(f"[Browser] {name}.requires_confirmation == False",
          t is not None and t.requires_confirmation is False,
          f"got {t.requires_confirmation if t else 'None'}")


# ============================================================================
# 7. DELETE_MEMORY in groq_tool_schemas
# ============================================================================
print("\n=== 7. DELETE_MEMORY in groq_tool_schemas ===")

chat_schemas = tool_registry.groq_tool_schemas(chat_only=True)
schema_names = [s["function"]["name"] for s in chat_schemas]
check("DELETE_MEMORY in chat-only groq schemas", "DELETE_MEMORY" in schema_names)
check("ANALYZE_IMAGE NOT in chat schemas", "ANALYZE_IMAGE" not in schema_names)

dm_schema = next((s for s in chat_schemas if s["function"]["name"] == "DELETE_MEMORY"), None)
check("DELETE_MEMORY schema has confirm param", dm_schema is not None and
      "confirm" in dm_schema["function"]["parameters"]["properties"])


# ============================================================================
# 8. _execute_tool_in_loop handles DELETE_MEMORY
# ============================================================================
print("\n=== 8. _execute_tool_in_loop: DELETE_MEMORY ===")

# Without user_id: returns ok=False with a clear error (login required)
result_no_user = main._execute_tool_in_loop("DELETE_MEMORY", {"confirm": True, "key": "__all__"})
check("DELETE_MEMORY without user_id returns ok=False (login required)",
      result_no_user.get("ok") is False, str(result_no_user))
check("DELETE_MEMORY error message present",
      bool(result_no_user.get("error")), str(result_no_user))

# With a mock user_id but Supabase unavailable: returns ok=False from delete_all_memories
# (DB is not configured in test env — that's expected)
result_with_user = main._execute_tool_in_loop(
    "DELETE_MEMORY", {"confirm": True, "key": "__all__"}, user_id="test-user-uuid"
)
check("DELETE_MEMORY with user_id returns a dict with 'ok' key",
      "ok" in result_with_user, str(result_with_user))
check("DELETE_MEMORY with user_id returns a message",
      bool(result_with_user.get("message")), str(result_with_user))


# ============================================================================
# 9. ALL_TOOL_NAMES contains SEND_SMS and SEND_MESSAGE
# ============================================================================
print("\n=== 9. ALL_TOOL_NAMES contains aliases ===")

check("SEND_SMS in ALL_TOOL_NAMES",     "SEND_SMS"     in main.ALL_TOOL_NAMES)
check("SEND_MESSAGE in ALL_TOOL_NAMES", "SEND_MESSAGE" in main.ALL_TOOL_NAMES)
check("DELETE_MEMORY in ALL_TOOL_NAMES","DELETE_MEMORY" in main.ALL_TOOL_NAMES)
check("CALL_CONTACT in ALL_TOOL_NAMES", "CALL_CONTACT"  in main.ALL_TOOL_NAMES)


# ============================================================================
# 10. SEND_MESSAGE and SEND_SMS share the same Pydantic param model
# ============================================================================
print("\n=== 10. SEND_MESSAGE and SEND_SMS share param model ===")

sm = android_tools.get_android_tool("SEND_MESSAGE")
ss = android_tools.get_android_tool("SEND_SMS")
check("same param_model class", sm is not None and ss is not None and
      sm.param_model is ss.param_model)

# Validate a message through SEND_MESSAGE
ok, err, model = sm.validate_parameters({"phone_number": "+911234567890", "message_body": "Hello!"})
check("SEND_MESSAGE valid params -> ok=True", ok, err)

ok, err, model = sm.validate_parameters({"phone_number": "", "message_body": "Hello!"})
check("SEND_MESSAGE empty phone -> ok=False", not ok)

ok, err, model = sm.validate_parameters({"phone_number": "+91123", "message_body": ""})
check("SEND_MESSAGE empty body -> ok=False", not ok)


# ============================================================================
# 11. groq_android_tools_schema includes SEND_MESSAGE
# ============================================================================
print("\n=== 11. groq_android_tools_schema includes SEND_MESSAGE ===")

android_schemas = android_tools.groq_android_tools_schema()
android_names   = [s["function"]["name"] for s in android_schemas]
check("SEND_MESSAGE in android groq schemas", "SEND_MESSAGE" in android_names)
check("SEND_SMS in android groq schemas",     "SEND_SMS"     in android_names)
check("CALL_CONTACT in android groq schemas", "CALL_CONTACT" in android_names)


# ============================================================================
# 12. No fake phone call — CALL_CONTACT default is ACTION_DIAL
# ============================================================================
print("\n=== 12. CALL_CONTACT defaults to ACTION_DIAL (no auto-call) ===")

from android_tools import CallContactParams

params = CallContactParams(contact_name="Priya")
check("use_action_call default is False", params.use_action_call is False)

src = open("android_tools.py").read()
check("description mentions ACTION_DIAL not auto-call",
      "ACTION_DIAL" in android_tools.ANDROID_TOOL_REGISTRY["CALL_CONTACT"].description)
check("description says user must press",
      "User must press" in android_tools.ANDROID_TOOL_REGISTRY["CALL_CONTACT"].description
      or "user must press" in android_tools.ANDROID_TOOL_REGISTRY["CALL_CONTACT"].description.lower())


# ============================================================================
# Summary
# ============================================================================
print(f"\n{'=' * 55}")
if errors == 0:
    print("ALL CONFIRMATION FLOW TESTS PASSED")
else:
    print(f"{errors} TEST(S) FAILED")
sys.exit(1 if errors > 0 else 0)
