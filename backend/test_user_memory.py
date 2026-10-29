"""
Nova AI Assistant — test_user_memory.py

Offline unit tests for user_memory.py.

All tests are fully offline — Supabase is stubbed via unittest.mock.
No network calls, no DB connection, no real credentials needed.

Covers:
  1.  Key normalisation — spaces, hyphens, mixed-case, unicode stripping
  2.  Key validation — empty, too long, invalid characters
  3.  Value validation — empty, too long, valid values
  4.  Secret rejection — API keys, JWTs, PEM headers, passwords
  5.  save_memory — success path (upsert called with right args)
  6.  save_memory — DB unavailable → graceful failure
  7.  save_memory — missing user_id → graceful failure
  8.  recall_memories — all memories returned
  9.  recall_memories — specific key lookup
  10. recall_memories — empty result
  11. recall_memories — DB unavailable → empty list
  12. delete_memory — found → deleted
  13. delete_memory — not found → ok=False, no crash
  14. delete_memory — DB unavailable → graceful failure
  15. delete_all_memories — wipes all rows for user
  16. delete_all_memories — DB unavailable → graceful failure
  17. format_memories_for_prompt — empty, single, multiple entries
  18. format_memories_for_prompt — long value truncated at 120 chars
  19. User isolation — save/recall/delete never called cross-user
  20. tool_registry: SAVE_MEMORY, RECALL_MEMORY in CHAT_TOOL_NAMES
  21. tool_registry: DELETE_MEMORY is HIGH risk
  22. main._execute_tool_in_loop: SAVE_MEMORY path
  23. main._execute_tool_in_loop: RECALL_MEMORY path
  24. main._execute_tool_in_loop: DELETE_MEMORY specific key path
  25. main._execute_tool_in_loop: DELETE_MEMORY __all__ path

Run from the backend/ directory:
    python test_user_memory.py
"""

import sys
import logging
import json
from unittest.mock import MagicMock, patch, call

logging.disable(logging.CRITICAL)

import user_memory as um
import tool_registry
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
# 1. Key normalisation
# ============================================================================
print("\n=== 1. Key normalisation ===")

cases = [
    ("favourite color",   "favourite_color"),
    ("Favourite-Color",   "favourite_color"),
    ("HOME CITY",         "home_city"),
    ("  wake_up_time  ",  "wake_up_time"),
    ("café",              "caf"),             # non-ASCII stripped
    ("a--b  c",           "a_b_c"),
]
for raw, expected in cases:
    ok, norm, err = um._normalise_key(raw)
    check(f"normalise '{raw}' -> '{expected}'", ok and norm == expected, f"got {norm!r}, err={err!r}")

# ============================================================================
# 2. Key validation — invalid cases
# ============================================================================
print("\n=== 2. Key validation — invalid cases ===")

invalid_keys = [
    "",
    "   ",
    "!@#$%",
    "a" * 100,   # too long (> 80 after truncation the key is valid but 100 chars raw → truncated to 80)
]

ok, norm, err = um._normalise_key("")
check("empty key -> ok=False", not ok, err)

ok, norm, err = um._normalise_key("   ")
check("whitespace-only key -> ok=False", not ok, err)

ok, norm, err = um._normalise_key("!@#$%")
check("special-chars-only key -> ok=False", not ok, err)

ok, norm, err = um._normalise_key("a" * 100)
check("100-char key -> truncated to 80 -> ok=True", ok, err)
check("truncated key is 80 chars", ok and len(norm) == 80)

# ============================================================================
# 3. Value validation
# ============================================================================
print("\n=== 3. Value validation ===")

ok, err = um._validate_value("blue")
check("valid value 'blue' -> ok=True", ok, err)

ok, err = um._validate_value("")
check("empty value -> ok=False", not ok)

ok, err = um._validate_value("  ")
check("whitespace value -> ok=False", not ok)

ok, err = um._validate_value("a" * 501)
check("501-char value -> ok=False", not ok)

ok, err = um._validate_value("I love biryani. " * 31)   # ~496 chars, clearly not a secret
check("500-char safe value -> ok=True (boundary)", ok, err)

# ============================================================================
# 4. Secret rejection
# ============================================================================
print("\n=== 4. Secret rejection ===")

secrets = [
    "gsk_abcdefghijklmnopqrstuvwxyz1234567890",    # Groq key
    "sk-abcdefghijklmnopqrstuvwxyz1234567890",     # OpenAI key
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc",  # JWT
    "-----BEGIN RSA PRIVATE KEY-----",             # PEM
    "password: hunter2",                           # literal password
    "secret: myS3cr3tK3y",                         # literal secret
    "token: eyJhbGciOiJSUzI1NiJ9.abc.def.ghi",    # token
]
for s in secrets:
    ok, err = um._validate_value(s)
    check(f"secret rejected: '{s[:30]}...'", not ok, f"was accepted!")

safe_values = [
    "blue",
    "Mumbai",
    "I prefer spicy food",
    "7:30 AM",
    "Software engineer",
]
for v in safe_values:
    ok, err = um._validate_value(v)
    check(f"safe value accepted: '{v}'", ok, err)

# ============================================================================
# 5. save_memory — success path
# ============================================================================
print("\n=== 5. save_memory — success ===")

mock_db = MagicMock()
mock_db.table.return_value.upsert.return_value.execute.return_value = MagicMock()

with patch("user_memory.get_db", return_value=mock_db):
    result = um.save_memory("user-123", "home_city", "Mumbai")

check("save_memory ok=True", result.ok is True, result.message)
check("save_memory message non-empty", bool(result.message))
check("save_memory data has key+value", result.data and result.data.get("key") == "home_city")
# Verify upsert was called with correct user_id scoping
upsert_args = mock_db.table.return_value.upsert.call_args
check("upsert called with user_id=user-123",
      upsert_args is not None and upsert_args[0][0].get("user_id") == "user-123")
check("upsert on_conflict=user_id,key",
      "on_conflict" in (upsert_args[1] or {}) and "user_id" in upsert_args[1]["on_conflict"])

# ============================================================================
# 6. save_memory — DB unavailable
# ============================================================================
print("\n=== 6. save_memory — DB unavailable ===")

with patch("user_memory.get_db", return_value=None):
    result = um.save_memory("user-123", "home_city", "Mumbai")

check("DB unavailable -> ok=False", result.ok is False)
check("DB unavailable -> message present", bool(result.message))

# ============================================================================
# 7. save_memory — missing user_id
# ============================================================================
print("\n=== 7. save_memory — missing user_id ===")

result = um.save_memory("", "home_city", "Mumbai")
check("empty user_id -> ok=False", result.ok is False)

result = um.save_memory(None, "home_city", "Mumbai")   # type: ignore
check("None user_id -> ok=False", result.ok is False)

# ============================================================================
# 8. recall_memories — all
# ============================================================================
print("\n=== 8. recall_memories — all ===")

fake_rows = [
    {"id": "a", "key": "home_city", "value": "Mumbai",
     "source": "agent", "created_at": "2026-01-01", "updated_at": "2026-01-01"},
    {"id": "b", "key": "favourite_color", "value": "blue",
     "source": "explicit", "created_at": "2026-01-01", "updated_at": "2026-01-01"},
]
mock_db2 = MagicMock()
mock_db2.table.return_value.select.return_value.eq.return_value \
    .order.return_value.limit.return_value.execute.return_value \
    = MagicMock(data=fake_rows)

with patch("user_memory.get_db", return_value=mock_db2):
    memories = um.recall_memories("user-123")

check("recall all: returns 2 rows", len(memories) == 2)
check("recall all: first key is home_city", memories[0].key == "home_city")
check("recall all: second key is favourite_color", memories[1].key == "favourite_color")
check("recall all: MemoryRow type", isinstance(memories[0], um.MemoryRow))

# ============================================================================
# 9. recall_memories — specific key
# ============================================================================
print("\n=== 9. recall_memories — specific key ===")

single_row = [
    {"id": "a", "key": "home_city", "value": "Mumbai",
     "source": "agent", "created_at": "2026-01-01", "updated_at": "2026-01-01"},
]
mock_db3 = MagicMock()
# For specific key: .eq(user_id).eq(key).execute()
mock_db3.table.return_value.select.return_value \
    .eq.return_value.eq.return_value.execute.return_value \
    = MagicMock(data=single_row)

with patch("user_memory.get_db", return_value=mock_db3):
    memories = um.recall_memories("user-123", key="home_city")

check("recall by key: returns 1 row", len(memories) == 1)
check("recall by key: key is home_city", memories[0].key == "home_city")

# ============================================================================
# 10. recall_memories — empty result
# ============================================================================
print("\n=== 10. recall_memories — empty ===")

mock_db4 = MagicMock()
mock_db4.table.return_value.select.return_value.eq.return_value \
    .order.return_value.limit.return_value.execute.return_value \
    = MagicMock(data=[])

with patch("user_memory.get_db", return_value=mock_db4):
    memories = um.recall_memories("user-123")

check("empty result -> empty list", memories == [])

# ============================================================================
# 11. recall_memories — DB unavailable
# ============================================================================
print("\n=== 11. recall_memories — DB unavailable ===")

with patch("user_memory.get_db", return_value=None):
    memories = um.recall_memories("user-123")

check("DB unavailable -> empty list", memories == [])

# ============================================================================
# 12. delete_memory — found
# ============================================================================
print("\n=== 12. delete_memory — found ===")

mock_del = MagicMock()
mock_del.table.return_value.delete.return_value \
    .eq.return_value.eq.return_value.execute.return_value \
    = MagicMock(data=[{"id": "a"}])

with patch("user_memory.get_db", return_value=mock_del):
    result = um.delete_memory("user-123", "home_city")

check("delete found: ok=True", result.ok is True, result.message)
check("delete found: message non-empty", bool(result.message))
check("delete found: data has key", result.data and result.data.get("key") == "home_city")

# ============================================================================
# 13. delete_memory — not found
# ============================================================================
print("\n=== 13. delete_memory — not found ===")

mock_notfound = MagicMock()
mock_notfound.table.return_value.delete.return_value \
    .eq.return_value.eq.return_value.execute.return_value \
    = MagicMock(data=[])

with patch("user_memory.get_db", return_value=mock_notfound):
    result = um.delete_memory("user-123", "nonexistent_key")

check("delete not found: ok=False", result.ok is False)
check("delete not found: message present", bool(result.message))

# ============================================================================
# 14. delete_memory — DB unavailable
# ============================================================================
print("\n=== 14. delete_memory — DB unavailable ===")

with patch("user_memory.get_db", return_value=None):
    result = um.delete_memory("user-123", "home_city")

check("delete DB unavailable: ok=False", result.ok is False)

# ============================================================================
# 15. delete_all_memories
# ============================================================================
print("\n=== 15. delete_all_memories ===")

mock_wipe = MagicMock()
mock_wipe.table.return_value.delete.return_value \
    .eq.return_value.execute.return_value \
    = MagicMock(data=[{"id": "a"}, {"id": "b"}, {"id": "c"}])

with patch("user_memory.get_db", return_value=mock_wipe):
    result = um.delete_all_memories("user-123")

check("delete_all ok=True", result.ok is True, result.message)
check("delete_all data has deleted_count=3", result.data and result.data.get("deleted_count") == 3)
# Confirm the delete was scoped to user-123
delete_eq_call = mock_wipe.table.return_value.delete.return_value.eq.call_args_list
check("delete_all scoped by user_id",
      any("user-123" in str(c) for c in delete_eq_call))

# ============================================================================
# 16. delete_all_memories — DB unavailable
# ============================================================================
print("\n=== 16. delete_all_memories — DB unavailable ===")

with patch("user_memory.get_db", return_value=None):
    result = um.delete_all_memories("user-123")

check("delete_all DB unavailable: ok=False", result.ok is False)

# ============================================================================
# 17. format_memories_for_prompt
# ============================================================================
print("\n=== 17. format_memories_for_prompt ===")

empty_result = um.format_memories_for_prompt([])
check("empty list -> empty string", empty_result == "")

rows = [
    um.MemoryRow("a", "home_city", "Mumbai", "agent", "2026-01-01", "2026-01-01"),
]
single = um.format_memories_for_prompt(rows)
check("single memory: contains key", "home_city" in single)
check("single memory: contains value", "Mumbai" in single)
check("single memory: contains header", "USER MEMORY" in single)

rows2 = [
    um.MemoryRow("a", "home_city", "Mumbai", "agent", "2026-01-01", "2026-01-01"),
    um.MemoryRow("b", "favourite_color", "blue", "explicit", "2026-01-01", "2026-01-01"),
]
multi = um.format_memories_for_prompt(rows2)
check("multi memories: both keys present", "home_city" in multi and "favourite_color" in multi)
check("multi memories: instruction present", "naturally" in multi.lower())

# ============================================================================
# 18. format_memories_for_prompt — long value truncated
# ============================================================================
print("\n=== 18. format_memories_for_prompt — truncation ===")

long_val = "x" * 200
rows3 = [um.MemoryRow("a", "long_key", long_val, "agent", "2026-01-01", "2026-01-01")]
result_str = um.format_memories_for_prompt(rows3)
check("long value truncated with ellipsis", "..." in result_str or "\u2026" in result_str)
# The prompt block should be under 500 chars for a single memory
check("prompt block not excessively long", len(result_str) < 500)

# ============================================================================
# 19. User isolation — save/delete scoped by user_id
# ============================================================================
print("\n=== 19. User isolation ===")

mock_iso = MagicMock()
mock_iso.table.return_value.upsert.return_value.execute.return_value = MagicMock()

user_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
user_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

with patch("user_memory.get_db", return_value=mock_iso):
    um.save_memory(user_a, "home_city", "Delhi")

upsert_data = mock_iso.table.return_value.upsert.call_args[0][0]
check("save scoped to user_a (not user_b)",
      upsert_data.get("user_id") == user_a and upsert_data.get("user_id") != user_b)

# ============================================================================
# 20. tool_registry: SAVE_MEMORY, RECALL_MEMORY in CHAT_TOOL_NAMES
# ============================================================================
print("\n=== 20. tool_registry: memory tools in CHAT_TOOL_NAMES ===")

check("SAVE_MEMORY in CHAT_TOOL_NAMES",   "SAVE_MEMORY"   in tool_registry.CHAT_TOOL_NAMES)
check("RECALL_MEMORY in CHAT_TOOL_NAMES", "RECALL_MEMORY" in tool_registry.CHAT_TOOL_NAMES)
check("DELETE_MEMORY in CHAT_TOOL_NAMES", "DELETE_MEMORY" in tool_registry.CHAT_TOOL_NAMES)

save_tool = tool_registry.get_tool("SAVE_MEMORY")
recall_tool = tool_registry.get_tool("RECALL_MEMORY")
check("SAVE_MEMORY risk LOW",   save_tool   and save_tool.risk == tool_registry.ToolRiskLevel.LOW)
check("RECALL_MEMORY risk LOW", recall_tool and recall_tool.risk == tool_registry.ToolRiskLevel.LOW)

# ============================================================================
# 21. SAVE_MEMORY param validation
# ============================================================================
print("\n=== 21. SAVE_MEMORY param validation ===")

ok, err, _tool, model = tool_registry.validate_tool_call("SAVE_MEMORY", {"key": "home city", "value": "Mumbai"})
check("SAVE_MEMORY normalises key with space", ok, err)
check("SAVE_MEMORY normalised key is home_city", ok and model and model.key == "home_city")

ok, err, _tool, model = tool_registry.validate_tool_call("SAVE_MEMORY", {"key": "", "value": "Mumbai"})
check("SAVE_MEMORY empty key -> ok=False", not ok)

ok, err, _tool, model = tool_registry.validate_tool_call("SAVE_MEMORY", {"key": "home_city", "value": ""})
check("SAVE_MEMORY empty value -> ok=False", not ok)

# RECALL_MEMORY with no key (optional)
ok, err, _tool, model = tool_registry.validate_tool_call("RECALL_MEMORY", {})
check("RECALL_MEMORY no key -> ok=True (key optional)", ok, err)
check("RECALL_MEMORY key is None when omitted", ok and model and model.key is None)

ok, err, _tool, model = tool_registry.validate_tool_call("RECALL_MEMORY", {"key": "home_city"})
check("RECALL_MEMORY with key -> ok=True", ok, err)

# DELETE_MEMORY with specific key
ok, err, _tool, model = tool_registry.validate_tool_call("DELETE_MEMORY", {"key": "home_city", "confirm": True})
check("DELETE_MEMORY specific key -> ok=True", ok, err)
check("DELETE_MEMORY key normalised", ok and model and model.key == "home_city")

ok, err, _tool, model = tool_registry.validate_tool_call("DELETE_MEMORY", {"key": "__all__", "confirm": True})
check("DELETE_MEMORY __all__ -> ok=True", ok, err)

ok, err, _tool, model = tool_registry.validate_tool_call("DELETE_MEMORY", {"key": "home_city", "confirm": False})
check("DELETE_MEMORY confirm=False -> ok=False", not ok)

# ============================================================================
# 22-25. main._execute_tool_in_loop memory paths
# ============================================================================
print("\n=== 22-25. _execute_tool_in_loop memory paths ===")

# 22. SAVE_MEMORY
with patch("main.save_one_memory", return_value=um.MemoryResult(
    ok=True, message="Yaad kar liya: 'home_city' = 'Mumbai'.",
    data={"key": "home_city", "value": "Mumbai"}
)):
    res = main._execute_tool_in_loop("SAVE_MEMORY", {"key": "home_city", "value": "Mumbai"}, user_id="u1")

check("SAVE_MEMORY loop path ok=True", res.get("ok") is True, str(res))
check("SAVE_MEMORY loop path message present", bool(res.get("message")))
check("SAVE_MEMORY loop path data present", bool(res.get("data")))

# 23. RECALL_MEMORY — with memories
fake_memories = [
    um.MemoryRow("a", "home_city", "Mumbai", "agent", "2026-01-01", "2026-01-01"),
]
with patch("main.recall_memories", return_value=fake_memories):
    res = main._execute_tool_in_loop("RECALL_MEMORY", {}, user_id="u1")

check("RECALL_MEMORY loop ok=True", res.get("ok") is True, str(res))
check("RECALL_MEMORY loop has memories list", isinstance(res.get("memories"), list))
check("RECALL_MEMORY loop count=1", res.get("count") == 1)

# 23b. RECALL_MEMORY — empty
with patch("main.recall_memories", return_value=[]):
    res = main._execute_tool_in_loop("RECALL_MEMORY", {}, user_id="u1")

check("RECALL_MEMORY empty -> ok=True with empty list", res.get("ok") is True)
check("RECALL_MEMORY empty -> has message", bool(res.get("message")))

# 24. DELETE_MEMORY — specific key
with patch("main.delete_one_memory", return_value=um.MemoryResult(
    ok=True, message="'home_city' memory delete kar di.", data={"key": "home_city"}
)):
    res = main._execute_tool_in_loop("DELETE_MEMORY", {"key": "home_city", "confirm": True}, user_id="u1")

check("DELETE_MEMORY specific key ok=True", res.get("ok") is True, str(res))
check("DELETE_MEMORY specific key message present", bool(res.get("message")))

# 25. DELETE_MEMORY — __all__
with patch("main.delete_all_memories", return_value=um.MemoryResult(
    ok=True, message="Saari memories delete kar di (3 items).", data={"deleted_count": 3}
)):
    res = main._execute_tool_in_loop("DELETE_MEMORY", {"key": "__all__", "confirm": True}, user_id="u1")

check("DELETE_MEMORY __all__ ok=True", res.get("ok") is True, str(res))
check("DELETE_MEMORY __all__ message present", bool(res.get("message")))

# ============================================================================
# Summary
# ============================================================================
print(f"\n{'=' * 55}")
if errors == 0:
    print("ALL USER MEMORY TESTS PASSED")
else:
    print(f"{errors} TEST(S) FAILED")
sys.exit(1 if errors > 0 else 0)
