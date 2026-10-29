"""
Nova AI Assistant — test_tool_registry.py

Quick verification that the tool registry refactor is correct:
1. tool_registry.py imports without tools.py
2. tools.py shim re-exports work
3. validate_tool_call accepts valid params and rejects invalid ones
4. groq_tool_schemas marks optional fields correctly
5. Server (FastAPI app) boots without import errors

Run from the backend/ directory:
    python test_tool_registry.py
"""

import sys
import importlib

PASS = "[PASS]"
FAIL = "[FAIL]"
errors = 0

def check(label, ok, detail=""):
    global errors
    if ok:
        print(f"  {PASS} {label}")
    else:
        errors += 1
        print(f"  {FAIL} {label}  — {detail}")

# ---------------------------------------------------------------------------
# Test 1: tool_registry imports with no tools.py dependency
# ---------------------------------------------------------------------------
print("\n=== 1. tool_registry is self-contained ===")

# Remove tools from sys.modules to simulate a fresh import
for mod in list(sys.modules):
    if mod in ("tools", "tool_registry"):
        del sys.modules[mod]

import tool_registry

check(
    "build_system_prompt lives in tool_registry",
    hasattr(tool_registry, "build_system_prompt"),
)
sp = tool_registry.build_system_prompt()
check("system prompt is a non-empty string", isinstance(sp, str) and len(sp) > 50)

check("__all__ is defined", hasattr(tool_registry, "__all__"))
check("build_system_prompt in __all__", "build_system_prompt" in tool_registry.__all__)

# Verify tool_registry module does NOT import tools
import inspect
source = inspect.getsource(tool_registry)
check("no 'from tools import' in source", "from tools import" not in source)
check("no 'import tools' in source", "import tools" not in source)

# ---------------------------------------------------------------------------
# Test 2: tools.py shim re-exports work
# ---------------------------------------------------------------------------
print("\n=== 2. tools.py backward-compat shim ===")

import tools as tools_shim

check("tools.TOOL_REGISTRY exists", hasattr(tools_shim, "TOOL_REGISTRY"))
check("tools.get_tool exists", hasattr(tools_shim, "get_tool"))
check("tools.build_system_prompt exists", hasattr(tools_shim, "build_system_prompt"))
check("tools.all_tool_schemas exists", hasattr(tools_shim, "all_tool_schemas"))
check("tools.groq_tool_schemas exists", hasattr(tools_shim, "groq_tool_schemas"))

# Old plural aliases
check("tools.all_tools_schema alias exists", hasattr(tools_shim, "all_tools_schema"))
check("tools.groq_tools_schema alias exists", hasattr(tools_shim, "groq_tools_schema"))

# Aliases point to same function
check(
    "all_tools_schema is all_tool_schemas",
    tools_shim.all_tools_schema is tool_registry.all_tool_schemas,
)
check(
    "groq_tools_schema is groq_tool_schemas",
    tools_shim.groq_tools_schema is tool_registry.groq_tool_schemas,
)

# ---------------------------------------------------------------------------
# Test 3: validate_tool_call — valid + invalid cases
# ---------------------------------------------------------------------------
print("\n=== 3. validate_tool_call ===")

from tool_registry import validate_tool_call, ALL_TOOL_NAMES, CHAT_TOOL_NAMES

check("SEARCH_YOUTUBE in ALL_TOOL_NAMES", "SEARCH_YOUTUBE" in ALL_TOOL_NAMES)
check("ANALYZE_IMAGE in ALL_TOOL_NAMES", "ANALYZE_IMAGE" in ALL_TOOL_NAMES)
check("ANALYZE_IMAGE NOT in CHAT_TOOL_NAMES", "ANALYZE_IMAGE" not in CHAT_TOOL_NAMES)

# Valid: PLAY_YOUTUBE
ok, err, tool, model = validate_tool_call("PLAY_YOUTUBE", {"query": "Arijit Singh"})
check("PLAY_YOUTUBE valid query -> ok=True", ok, err)
check("PLAY_YOUTUBE returns ToolDefinition", tool is not None and tool.name == "PLAY_YOUTUBE")
check("PLAY_YOUTUBE returns validated model", model is not None and model.query == "Arijit Singh")

# Invalid: empty query
ok, err, tool, model = validate_tool_call("PLAY_YOUTUBE", {"query": ""})
check("PLAY_YOUTUBE empty query -> ok=False", not ok)
check("PLAY_YOUTUBE empty query returns tool def", tool is not None)
check("PLAY_YOUTUBE empty query returns None model", model is None)

# Valid: OPEN_WEBSITE
ok, err, tool, model = validate_tool_call("OPEN_WEBSITE", {"url": "google.com"})
check("OPEN_WEBSITE bare domain -> ok=True", ok, err)
check("OPEN_WEBSITE prepends https://", model.url.startswith("https://"))

# Invalid: javascript: URL
ok, err, tool, model = validate_tool_call("OPEN_WEBSITE", {"url": "javascript:alert(1)"})
check("OPEN_WEBSITE javascript: -> ok=False", not ok)

# Valid: GET_WEATHER
ok, err, tool, model = validate_tool_call("GET_WEATHER", {"city": "Mumbai"})
check("GET_WEATHER valid city -> ok=True", ok, err)

# Invalid: city with shell chars
ok, err, tool, model = validate_tool_call("GET_WEATHER", {"city": "Mumbai; rm -rf /"})
check("GET_WEATHER shell chars -> ok=False", not ok)

# Unknown tool name
ok, err, tool, model = validate_tool_call("FLY_ROCKET", {"fuel": "RP-1"})
check("Unknown tool -> ok=False", not ok)
check("Unknown tool -> tool=None", tool is None)

# ---------------------------------------------------------------------------
# Test 4: groq_tool_schemas required/optional fields
# ---------------------------------------------------------------------------
print("\n=== 4. groq_tool_schemas required/optional logic ===")

from tool_registry import groq_tool_schemas

schemas = groq_tool_schemas(chat_only=True)

# Chat-only: ANALYZE_IMAGE should NOT be present
chat_names = [s["function"]["name"] for s in schemas]
check("ANALYZE_IMAGE excluded from chat schemas", "ANALYZE_IMAGE" not in chat_names)
check("SEARCH_YOUTUBE in chat schemas", "SEARCH_YOUTUBE" in chat_names)
check("PLAY_YOUTUBE in chat schemas", "PLAY_YOUTUBE" in chat_names)
check("OPEN_WEBSITE in chat schemas", "OPEN_WEBSITE" in chat_names)
check("GET_WEATHER in chat schemas", "GET_WEATHER" in chat_names)

# PLAY_YOUTUBE: query is required
yt_schema = next(s for s in schemas if s["function"]["name"] == "PLAY_YOUTUBE")
yt_required = yt_schema["function"]["parameters"]["required"]
check("PLAY_YOUTUBE query is required", "query" in yt_required)

# OPEN_WEBSITE: url is required
ow_schema = next(s for s in schemas if s["function"]["name"] == "OPEN_WEBSITE")
ow_required = ow_schema["function"]["parameters"]["required"]
check("OPEN_WEBSITE url is required", "url" in ow_required)

# ANALYZE_IMAGE (chat_only=False): prompt has a default → NOT required
all_schemas = groq_tool_schemas(chat_only=False)
ai_schema = next(s for s in all_schemas if s["function"]["name"] == "ANALYZE_IMAGE")
ai_required = ai_schema["function"]["parameters"]["required"]
check("ANALYZE_IMAGE prompt NOT required (has default)", "prompt" not in ai_required)

# ---------------------------------------------------------------------------
# Test 5: Server boot (FastAPI app imports without error)
# ---------------------------------------------------------------------------
print("\n=== 5. Server boot (main:app import) ===")

try:
    import main
    check("main module imported successfully", True)
    check("main.app is a FastAPI instance", hasattr(main, "app"))
    check("main.SYSTEM_PROMPT is non-empty", isinstance(main.SYSTEM_PROMPT, str) and len(main.SYSTEM_PROMPT) > 50)
    check("main.ALL_TOOL_NAMES is a set", isinstance(main.ALL_TOOL_NAMES, set))
    check("main.ALL_TOOL_NAMES includes Android tools", "CALL_CONTACT" in main.ALL_TOOL_NAMES)
except Exception as e:
    check("main module imported successfully", False, str(e))

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print(f"\n{'='*50}")
if errors == 0:
    print("ALL TESTS PASSED")
else:
    print(f"{errors} TEST(S) FAILED")
sys.exit(1 if errors > 0 else 0)
