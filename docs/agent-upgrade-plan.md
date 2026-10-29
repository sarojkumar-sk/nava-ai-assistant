# Nova AI Assistant → Controlled AI Agent — Implementation Plan

> **Status:** Planning only. No code changes.
> **Date:** 2026-10-27
> **Stack:** FastAPI + Groq SDK + vanilla JS frontend

---

## Executive Summary

Nova currently makes a **single** Groq call per user message, gets back at most one tool call, dispatches it, and returns. To become a true agent, Nova needs an **agent loop** on the backend: call the LLM → if it returns a tool call, execute the tool → feed the result back to the LLM → repeat until the LLM produces a final text reply or a step/timeout limit is hit.

The upgrade is designed as a **backend-only change** with minimal frontend touch-points. The existing `/chat` endpoint signature stays the same. The frontend continues to send `{ message, conversation_id, user_name }` and receive the same response shapes — but the response may now include `agent_steps` (a trace of what Nova did).

---

## 1. Tool Registry Design

### 1.1 Current State

Two separate, parallel registries exist:

| Registry | File | Tools | Execution |
|---|---|---|---|
| Browser tools | `tools.py` | `OPEN_WEBSITE`, `PLAY_YOUTUBE`, `GET_WEATHER` | Frontend JS (`script.js`) |
| Android tools | `android_tools.py` | `CALL_CONTACT`, `SEND_SMS`, `SET_ALARM`, `GET_CONTACTS` | Backend relay → Android app |

Each has its own `ToolDefinition` class, `validate_parameters()`, `groq_tools_schema()`, and `to_dict()`. They are structurally identical but duplicated.

### 1.2 Proposed Unified Registry

Create a single `agent_tools.py` that unifies both registries under one interface, without deleting the existing files (they become thin re-exports for backwards compat).

```
agent_tools.py
├── ToolRiskLevel (enum: LOW, MEDIUM, HIGH)
├── ToolCategory  (enum: BROWSER, ANDROID, SERVER)  # where it executes
├── ToolDefinition (unified)
│   ├── name: str
│   ├── description: str
│   ├── category: ToolCategory
│   ├── risk: ToolRiskLevel
│   ├── parameters_schema: dict          # shown to LLM
│   ├── param_model: Pydantic BaseModel  # validates params
│   ├── requires_confirmation: bool      # derived from risk level
│   ├── execute_server_side: callable?   # for SERVER tools only
│   ├── required_permissions: list[str]   # Android only
│   ├── timeout_s: int
│   └── validate_parameters(params) → (ok, error, validated)
├── TOOL_REGISTRY: dict[str, ToolDefinition]  # all tools, single source
├── get_tool(name) → ToolDefinition | None
├── all_tool_schemas() → list[dict]
├── groq_all_tool_schemas() → list[dict]   # OpenAI function-calling format
└── tools_for_request(device_online: bool) → list[dict]
    # returns browser tools always + android tools only if device online
```

### 1.3 Risk Levels

| Risk Level | Tools | Confirmation | Rationale |
|---|---|---|---|
| **LOW** | `GET_WEATHER`, `GET_CONTACTS`, `PLAY_YOUTUBE` | No confirmation | Read-only or opens a passive page (YouTube search). No data leaves the user's browser/device. |
| **MEDIUM** | `OPEN_WEBSITE` | **Yes** — modal before acting | Opens an arbitrary URL. Existing behavior preserved. |
| **HIGH** | `CALL_CONTACT`, `SEND_SMS` | **Yes** — modal before acting | Affects the physical world (dialer, SMS app). Existing behavior preserved. |
| **LOW** | `SET_ALARM` | No confirmation | Opens Clock app with pre-filled alarm; user sees confirmation UI on device. |

`requires_confirmation` is derived from risk: `MEDIUM` and `HIGH` → `True`, `LOW` → `False`. This preserves the existing per-tool `requires_confirmation` flags exactly.

### 1.4 New Server-Side Tools (Phase 2 — optional)

The agent loop enables tools that execute on the backend and return data to the LLM for reasoning. These are **new** tools, additive only:

| Tool | Risk | Executes | Returns to LLM |
|---|---|---|---|
| `FETCH_WEATHER` | LOW | Backend (`wttr.in`) | JSON weather data |
| `SEARCH_WEB` | LOW | Backend (DuckDuckGo or similar) | Search result titles + snippets |
| `FETCH_URL` | MEDIUM | Backend (`httpx.get`) | Page text (sanitized, truncated) |

These are **optional** and not required for the agent loop to work. The plan below marks them as Phase 2. The agent loop works with the existing browser + Android tools first.

### 1.5 Migration Strategy

**Do not rewrite `tools.py` or `android_tools.py`.** Instead:

1. `agent_tools.py` imports from both and builds the unified registry.
2. `main.py` imports from `agent_tools.py` instead of importing from `tools.py` / `android_tools.py` directly.
3. The old files keep working — they export the same classes and functions. No downstream breakage.

```python
# agent_tools.py (conceptual)
from tools import TOOL_REGISTRY as BROWSER_TOOLS, ToolDefinition as BrowserToolDef
from android_tools import ANDROID_TOOL_REGISTRY as ANDROID_TOOLS, AndroidToolDefinition

# Build unified registry by wrapping existing definitions
TOOL_REGISTRY = {}
for name, td in BROWSER_TOOLS.items():
    TOOL_REGISTRY[name] = UnifiedToolDefinition(from_browser=td)
for name, td in ANDROID_TOOLS.items():
    TOOL_REGISTRY[name] = UnifiedToolDefinition(from_android=td)
```

---

## 2. Agent Loop

### 2.1 Current Flow (Single-Shot)

```
User message
    → call_groq(model, message)          # single LLM call
    → parse_llm_response(result)         # parse
    → return to frontend                 # done
```

### 2.2 Proposed Flow (Agent Loop)

```
User message
    → agent_run(message, user, conversation_id)
        │
        ├─ Step 1: call_groq(tools, message + history)
        │   └─ If text reply → return {type: "message", response, agent_steps: [...]}
        │   └─ If tool_call → go to Step 2
        │
        ├─ Step 2: Validate + classify tool (browser/android/server)
        │   └─ If requires_confirmation → return {type: "tool_call", ...} to frontend
        │       (frontend confirms, then calls POST /agent/continue)
        │   └─ If no confirmation needed → execute immediately → go to Step 3
        │
        ├─ Step 3: Execute tool
        │   ├─ Browser tool → return {type: "tool_call"} to frontend
        │   │   (frontend executes in JS, calls POST /agent/continue with result)
        │   ├─ Android tool → dispatch via /device/command, poll /device/result
        │   │   → feed result back as tool_result message
        │   └─ Server tool → execute_server_side() → get result directly
        │
        ├─ Step 4: Feed tool result back to LLM
        │   messages.append(tool_call)
        │   messages.append({role: "tool", content: result_json})
        │   → Back to Step 1 (increment step counter)
        │
        └─ Loop ends when:
            ├─ LLM returns text (final answer) → return to frontend
            ├─ Step count > MAX_STEPS (default 5) → return "too many steps" message
            ├─ Total elapsed > TIMEOUT_S (default 30s) → return "timeout" message
            └─ Unrecoverable error → return error message
```

### 2.3 Key Parameters

| Parameter | Default | Env Var | Description |
|---|---|---|---|
| `MAX_STEPS` | 5 | `AGENT_MAX_STEPS` | Maximum LLM round-trips per user message |
| `TIMEOUT_S` | 30 | `AGENT_TIMEOUT_S` | Wall-clock timeout for the entire agent run |
| `TOOL_TIMEOUT_S` | 15 | `AGENT_TOOL_TIMEOUT_S` | Per-tool execution timeout |

### 2.4 Conversation Messages for the Loop

The agent loop maintains a `messages` list for the Groq call:

```python
messages = [
    {"role": "system", "content": system_prompt},
    # ── Conversation history (from Supabase, if logged in) ──
    {"role": "user", "content": "previous user msg"},
    {"role": "assistant", "content": "previous bot reply"},
    # ── Current turn ──
    {"role": "user", "content": user_message},
    # ── After tool execution, append: ──
    {"role": "assistant", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "GET_WEATHER", "arguments": '{"city":"Delhi"}'}}]},
    {"role": "tool", "tool_call_id": "call_1", "content": '{"temp_c":"28","description":"Partly cloudy"}'},
    # ── LLM sees tool result and produces final text or next tool call ──
]
```

### 2.5 Where the Loop Runs

**Backend (`main.py`)** — the loop is a single async function `agent_run()` that:

1. Calls Groq.
2. If tool call:
   - **Server tool** → execute immediately, feed result, loop.
   - **Browser tool** → return tool call to frontend. Frontend executes, then calls `POST /agent/continue` with the result. Backend feeds result to LLM, loops.
   - **Android tool** → dispatch via existing `/device/command`, poll `/device/result`, feed result, loop.
3. If text → return final response.
4. Enforce MAX_STEPS and TIMEOUT at each iteration.

This means the agent loop is a **stateful session** that can span multiple HTTP requests (for browser tools requiring confirmation / frontend execution). The state is stored in-memory keyed by an `agent_run_id`.

### 2.6 Agent Run State

```python
@dataclass
class AgentRunState:
    run_id: str               # UUID
    user_id: str | None
    conversation_id: str | None
    messages: list[dict]       # full LLM message history for this run
    step_count: int = 0
    started_at: float          # time.time()
    tool_call_id_counter: int  # for generating unique tool call IDs
    status: str = "active"     # active | completed | timed_out | error
```

Stored in an in-memory dict `_agent_runs: dict[str, AgentRunState]` with a TTL cleanup (same pattern as `device_session.py`). Entries auto-expire after 5 minutes of inactivity.

### 2.7 New Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/chat` | POST | **Unchanged signature.** Internally calls `agent_run()`. Returns same shapes + optional `agent_steps`. |
| `/agent/continue` | POST | Frontend calls this after executing a browser tool. Body: `{run_id, tool_call_id, result}`. Backend feeds result to LLM and continues the loop. Returns same shapes as `/chat`. |

The `/chat` endpoint is the entry point. If the first LLM call returns a tool call that needs frontend execution, `/chat` returns the tool call + a `run_id`. The frontend executes the tool, then calls `/agent/continue` to resume. This continues until a final text response is produced.

### 2.8 Error Recovery

| Error Type | Handling |
|---|---|
| Groq 429/500/502/503 | Use existing fallback model. If both fail, return friendly error. Loop does not retry — the fallback is one attempt. |
| Groq timeout | Same as above — try fallback model once. |
| Tool parameter validation fail | Feed error back to LLM as tool result: `{"error": "Invalid parameters: ..."}`. LLM can self-correct and retry with valid params. Counts as a step. |
| Tool execution failure (server-side) | Feed error back to LLM. LLM can choose a different tool or tell the user. |
| Android tool timeout | After `TOOL_TIMEOUT_S`, feed `{"error": "Device did not respond in time"}` to LLM. |
| Browser tool — frontend fails | Frontend calls `/agent/continue` with `{"error": "..."}`. LLM adapts. |
| MAX_STEPS exceeded | Return: `"Mujhe yeh task complete karne mein zyada steps lag gaye. Could you simplify the request?"` |
| TIMEOUT_S exceeded | Return: `"Yeh task time out ho gaya. Thoda simplify karke try karo."` |
| Malformed LLM tool call | Feed `{"error": "Malformed tool call"}` back to LLM. One retry. If still malformed, return error to user. |

### 2.9 Agent Steps Trace

Every response includes an optional `agent_steps` array (only if >1 step was taken):

```json
{
  "type": "message",
  "response": "Delhi ka temperature 28°C hai, partly cloudy.",
  "conversation_id": "uuid",
  "agent_steps": [
    {"step": 1, "action": "tool_call", "tool": "GET_WEATHER", "params": {"city": "Delhi"}},
    {"step": 2, "action": "tool_result", "tool": "GET_WEATHER", "result": "28°C, partly cloudy"},
    {"step": 3, "action": "final_reply"}
  ]
}
```

The frontend can display this as a collapsible "What Nova did" trace under the response.

---

## 3. Low-Risk vs High-Risk Tools + Confirmation

### 3.1 Risk Classification (Already Exists — Formalized)

```
LOW risk (no confirmation):
  - GET_WEATHER      (browser — read-only fetch)
  - GET_CONTACTS     (android — read contacts, no mutation)
  - SET_ALARM         (android — opens Clock app, user sees UI)
  - PLAY_YOUTUBE      (browser — opens search page)

MEDIUM risk (confirmation required):
  - OPEN_WEBSITE      (browser — opens arbitrary URL)

HIGH risk (confirmation required):
  - CALL_CONTACT      (android — opens dialer with number)
  - SEND_SMS          (android — opens SMS app pre-filled)

(Phase 2 server tools):
  - FETCH_WEATHER     (LOW — server-side read)
  - SEARCH_WEB        (LOW — server-side read)
  - FETCH_URL         (MEDIUM — server fetches arbitrary URL)
```

### 3.2 Confirmation Flow (Preserved + Extended)

**Current flow (preserved):**
1. Backend returns `{type: "tool_call", requires_confirmation: true}`.
2. Frontend shows `<dialog>` modal.
3. User confirms → frontend executes tool.
4. Frontend shows result in chat.

**Extended flow for agent loop:**
1. Backend returns `{type: "tool_call", requires_confirmation: true, run_id: "..."}`.
2. Frontend shows modal (same as today).
3. User confirms → frontend executes tool (same as today).
4. Frontend calls `POST /agent/continue` with `{run_id, tool_call_id, result: "executed"}`.
5. Backend feeds result to LLM → LLM may produce final text or call another tool.
6. If another tool call → back to step 1 (with new `run_id` — or same `run_id` if we keep state).

**If user denies confirmation:**
1. Frontend calls `POST /agent/continue` with `{run_id, tool_call_id, result: "cancelled"}`.
2. Backend feeds `"User cancelled this action"` to LLM.
3. LLM responds with something like "Theek hai, cancel kar diya 👍" or suggests an alternative.

### 3.3 Confirmation Modal Metadata

The existing `TOOL_CONFIRM_META` in `script.js` already covers `OPEN_WEBSITE`, `PLAY_YOUTUBE`, `GET_WEATHER`. Extend it:

```js
TOOL_CONFIRM_META['CALL_CONTACT'] = {
  icon: '📞',
  title: 'Call karna hai?',
  body: 'Nova yeh number dial karna chahti hai:',
  getDetail: (p) => p.phone_number || p.contact_name,
};
TOOL_CONFIRM_META['SEND_SMS'] = {
  icon: '💬',
  title: 'SMS bhejna hai?',
  body: 'SMS app is number ke liye open hoga:',
  getDetail: (p) => `${p.phone_number}\n"${p.message_body?.slice(0, 80)}…"`,
};
```

---

## 4. How Existing Features Are Preserved

### 4.1 Auth

**No changes to auth flow.**

- `auth_router.py` — unchanged. `verify_token` and `verify_token_optional` work as-is.
- `auth.js` — unchanged. `signUp`, `signIn`, `refreshSession`, `logout`, `requireAuth` all work.
- `login.html` / `signup.html` — unchanged.
- `/chat` continues using `verify_token_optional` — anonymous mode still works.
- `/agent/continue` uses the same dependency: `verify_token_optional`.
- The agent run state is scoped by `user_id` when available (same as conversation memory).

### 4.2 Chat

**`POST /chat` signature unchanged.**

- Request body: `{ message, conversation_id?, user_name? }` — same.
- Response body: `{ type, response?, intent?, parameters?, requires_confirmation?, conversation_id?, agent_steps? }` — same shapes, `agent_steps` is additive.
- Internally, `/chat` now calls `agent_run()` instead of `groq_with_fallback()` directly. But the external contract is identical.
- Conversation memory (`memory.py`) continues to save the final user message + final bot reply. Tool call intermediates are NOT saved to the messages table (same as current behavior — only the intent is saved).

### 4.3 Voice

**No changes to voice I/O.**

- `startListening()` / `stopListening()` — unchanged. Voice fills the input, calls `sendMessage()`.
- `speak(text)` — unchanged. Called after the final bot response.
- The agent loop is backend-only; the frontend never "speaks" intermediate tool calls. Only the final text response is spoken.

### 4.4 Image / Vision

**No changes to vision.**

- `POST /vision` — unchanged. Image analysis is a single-shot call, not part of the agent loop.
- `sendVision()` in `script.js` — unchanged.
- The vision model does not use tools. It only analyses the image and returns text.

### 4.5 YouTube

**`PLAY_YOUTUBE` tool preserved exactly.**

- Still a browser tool, executed via `window.open()` in `executePlayYoutube()`.
- In the agent loop, `PLAY_YOUTUBE` returns a tool call to the frontend (same as today).
- Frontend executes, calls `/agent/continue` with `{"result": "opened"}`.
- LLM receives the result and produces a final text reply (e.g., "YouTube par 'Arijit Singh songs' search kar diya, tab mein dekho").
- The existing confirmation metadata for `PLAY_YOUTUBE` is kept (though it's LOW risk / no confirmation — the metadata exists for display in the modal if ever needed).

### 4.6 Weather

**`GET_WEATHER` tool preserved.**

- Currently a browser tool that calls the backend `/weather` endpoint.
- In the agent loop, two options:
  - **Option A (minimal change):** Keep `GET_WEATHER` as a browser tool. Frontend fetches weather, calls `/agent/continue` with the weather JSON. LLM formats the final response.
  - **Option B (better for agent):** Add a `FETCH_WEATHER` server-side tool that executes on the backend (same `wttr.in` fetch, same code as the `/weather` endpoint). The LLM gets the raw weather JSON and formats a natural language reply. The frontend never calls `/weather` directly — the agent loop handles it.
- **Recommendation:** Option B for the agent loop, but keep `/weather` endpoint and `executeGetWeather()` for backwards compatibility (direct chat without agent loop can still use them).

### 4.7 Android Tools

**Android tool dispatch preserved.**

- The existing `/device/command`, `/device/poll`, `/device/result` routes are unchanged.
- In the agent loop, Android tools are dispatched by the backend (not the frontend):
  1. LLM calls `CALL_CONTACT`.
  2. Backend validates params, calls `push_command()` to queue the command.
  3. Backend polls `get_result()` (or uses a short async loop with `asyncio.sleep`).
  4. Result is fed back to the LLM as a tool result.
  5. LLM produces final text (e.g., "Dialer open ho gaya {number} ke liye. Green button press karo.").
- This is a **fix** — currently the Android tool dispatch from the frontend is a stub (just shows a message, never calls `/device/command`).

### 4.8 Rate Limiting

**Unchanged.** The existing `rate_limit_middleware` applies to all routes. `/agent/continue` is counted under the `/chat` bucket (path starts with `/agent`, which is not `/chat` or `/device` — it falls into the global bucket). Consider adding `/agent` to the chat rate limit bucket or creating a separate `AGENT_RPM` tier.

**Recommendation:** Add `AGENT_RPM` env var (default: same as `CHAT_RPM` = 20). Route `/agent/*` through the chat tier to prevent abuse.

---

## 5. File-by-File Change Plan

### 5.1 New Files

| File | Purpose | Priority |
|---|---|---|
| `backend/agent_tools.py` | Unified tool registry — imports from `tools.py` + `android_tools.py`, adds risk levels, `ToolCategory`, `groq_all_tool_schemas()`, `tools_for_request()` | P1 |
| `backend/agent_loop.py` | `AgentRunState` dataclass, `agent_run()` async function, `/agent/continue` handler logic, step/timeout enforcement, error recovery, in-memory run state store with TTL | P1 |
| `backend/agent_state.py` | In-memory store for `AgentRunState` objects. `_agent_runs: dict[str, AgentRunState]`, `create_run()`, `get_run()`, `update_run()`, `cleanup_expired()`. Same pattern as `device_session.py` but simpler (no persistence needed). | P1 |

### 5.2 Modified Files

| File | Changes | Priority |
|---|---|---|
| `backend/main.py` | (1) Replace `groq_with_fallback()` call in `/chat` with `agent_run()`. (2) Add `POST /agent/continue` endpoint. (3) Import from `agent_tools.py` instead of `tools.py` + `android_tools.py` directly. (4) Add `AGENT_RPM` to rate limiter. (5) Keep `groq_with_fallback()` as a helper used by `agent_loop.py`. (6) Add `agent_steps` to response. | P1 |
| `backend/tools.py` | **No changes.** Existing `ToolDefinition`, `TOOL_REGISTRY`, `groq_tools_schema()`, `build_system_prompt()` all remain. `agent_tools.py` imports from here. | — |
| `backend/android_tools.py` | **No changes.** Existing `AndroidToolDefinition`, `ANDROID_TOOL_REGISTRY`, `groq_android_tools_schema()` all remain. `agent_tools.py` imports from here. | — |
| `backend/android_router.py` | **No changes.** The agent loop calls `push_command()` / `get_result()` from `device_session.py` directly, not through the router. The router's HTTP endpoints remain for the Android app. | — |
| `backend/device_session.py` | **No changes.** `push_command()`, `get_result()`, `pop_pending_commands()` are called by `agent_loop.py`. | — |
| `backend/memory.py` | **No changes.** Conversation persistence works as-is. Only final user message + final bot reply are saved. | — |
| `backend/database.py` | **No changes.** | — |
| `backend/auth_router.py` | **No changes.** `verify_token` and `verify_token_optional` used as-is by new endpoints. | — |
| `backend/requirements.txt` | **No new dependencies.** Everything uses existing `groq`, `fastapi`, `pydantic`, `httpx`. | — |
| `backend/.env.example` | Add: `AGENT_MAX_STEPS=5`, `AGENT_TIMEOUT_S=30`, `AGENT_TOOL_TIMEOUT_S=15`, `AGENT_RPM=20` | P2 |
| `frontend/script.js` | (1) `handleServerResponse()` — handle `run_id` in tool call responses, call `POST /agent/continue` after tool execution. (2) Add `continueAgentRun(runId, toolCallId, result)` function. (3) Extend `TOOL_CONFIRM_META` with `CALL_CONTACT` + `SEND_SMS`. (4) Handle `agent_steps` display (collapsible trace). (5) `executeTool()` — after execution, call `continueAgentRun()` instead of returning. (6) Android tool calls — implement actual dispatch to `/device/command` + poll `/device/result` (currently a stub). | P1 |
| `frontend/index.html` | Add `agent_steps` display container (collapsible `<details>` element under bot messages). Minimal HTML addition. | P2 |
| `frontend/config.js` | **No changes.** `API_BASE_URL` used as-is. | — |
| `frontend/auth.js` | **No changes.** | — |
| `frontend/login.html` | **No changes.** | — |
| `frontend/signup.html` | **No changes.** | — |
| `frontend/style.css` | Add styles for `agent-steps` collapsible trace (minimal — reuses existing card styles). | P2 |

### 5.3 Android App (Kotlin)

| File | Changes | Priority |
|---|---|---|
| All `.kt` files | **No changes.** The Android app polls `/device/poll` and submits to `/device/result` as before. The agent loop interacts with the device through the same `device_session.py` functions. | — |

### 5.4 SQL Schemas

| File | Changes | Priority |
|---|---|---|
| `schema.sql` | **No changes.** | — |
| `schema_v2.sql` | **No changes.** | — |
| `profile_schema.sql` | **No changes.** | — |

---

## 6. Implementation Phases

### Phase 1 — Agent Loop Core (P1)

**Goal:** Multi-step reasoning with existing tools.

1. Create `agent_tools.py` — unified registry wrapping existing tools.
2. Create `agent_state.py` — in-memory run state store.
3. Create `agent_loop.py` — `agent_run()` function with step/timeout limits.
4. Modify `main.py`:
   - `/chat` calls `agent_run()` internally.
   - Add `POST /agent/continue` endpoint.
   - Add `AGENT_RPM` to rate limiter.
5. Modify `frontend/script.js`:
   - `handleServerResponse()` handles `run_id`.
   - Add `continueAgentRun()`.
   - `executeTool()` calls `continueAgentRun()` after execution.
   - Implement Android tool dispatch (fix the current stub).

**What works after Phase 1:**
- User: "Delhi ka mausam batao aur YouTube par 'rain sounds' play karo"
- Nova: Calls `GET_WEATHER` → gets result → calls `PLAY_YOUTUBE` → gets result → returns "Delhi mein 28°C hai, partly cloudy. Rain sounds YouTube par search kar diye, tab mein dekho."
- All existing single-tool requests continue to work identically.

### Phase 2 — Server-Side Tools + Polish (P2)

**Goal:** Tools that execute on the backend for better agent reasoning.

1. Add `FETCH_WEATHER` server-side tool to `agent_tools.py` (uses existing `wttr.in` fetch logic from `main.py`).
2. Add `agent_steps` trace display to `frontend/index.html` + `style.css`.
3. Update `.env.example` with agent config vars.
4. (Optional) Add `SEARCH_WEB` tool (DuckDuckGo Instant Answer API — free, no key).

### Phase 3 — Android Tool Dispatch Completion (P2)

**Goal:** Full Android tool round-trip in the agent loop.

1. Implement `dispatch_and_wait_for_device()` in `agent_loop.py`:
   - Call `push_command()`.
   - Async poll `get_result()` with `asyncio.sleep(0.5)` up to `TOOL_TIMEOUT_S`.
   - Feed result to LLM.
2. Test with `mock_device.py`.
3. Test with real Android app.

---

## 7. Sequence Diagrams

### 7.1 Simple Tool Call (No Confirmation)

```
Frontend                 Backend /chat              Groq
   │                         │                       │
   │  POST /chat             │                       │
   │  {message}              │                       │
   │────────────────────────>│                       │
   │                         │  call_groq(tools, msg) │
   │                         │──────────────────────>│
   │                         │  tool_call: GET_WEATHER│
   │                         │<──────────────────────│
   │                         │                       │
   │  {type: "tool_call",    │                       │
   │   intent: "GET_WEATHER",│                       │
   │   parameters: {...},    │                       │
   │   run_id: "abc123"}     │                       │
   │<────────────────────────│                       │
   │                         │                       │
   │  executeGetWeather()    │                       │
   │  (fetches /weather)     │                       │
   │                         │                       │
   │  POST /agent/continue   │                       │
   │  {run_id, result}       │                       │
   │────────────────────────>│                       │
   │                         │  call_groq(tools,      │
   │                         │    msg + tool_result) │
   │                         │──────────────────────>│
   │                         │  text: "28°C, cloudy"  │
   │                         │<──────────────────────│
   │                         │                       │
   │  {type: "message",      │                       │
   │   response: "28°C...",   │                       │
   │   agent_steps: [...]}   │                       │
   │<────────────────────────│                       │
```

### 7.2 Tool Call With Confirmation

```
Frontend                 Backend /chat              Groq
   │                         │                       │
   │  POST /chat             │                       │
   │  {message}              │                       │
   │────────────────────────>│                       │
   │                         │  call_groq()           │
   │                         │──────────────────────>│
   │                         │  tool_call: OPEN_WEBSITE
   │                         │<──────────────────────│
   │  {type: "tool_call",    │                       │
   │   requires_confirmation: true,                  │
   │   run_id: "abc123"}     │                       │
   │<────────────────────────│                       │
   │                         │                       │
   │  showConfirmation()      │                       │
   │  User clicks "Yes"      │                       │
   │  executeOpenWebsite()   │                       │
   │                         │                       │
   │  POST /agent/continue   │                       │
   │  {run_id, result: "opened"}                     │
   │────────────────────────>│                       │
   │                         │  call_groq()           │
   │                         │──────────────────────>│
   │                         │  text: "Website khul  │
   │                         │  gayi"                 │
   │                         │<──────────────────────│
   │  {type: "message",      │                       │
   │   response: "...",       │                       │
   │   agent_steps: [...]}   │                       │
   │<────────────────────────│                       │
```

### 7.3 Android Tool in Agent Loop

```
Frontend            Backend /chat       Groq          Device
   │                    │                  │             │
   │  POST /chat        │                  │             │
   │  "Call Mom"        │                  │             │
   │───────────────────>│                  │             │
   │                    │  call_groq()     │             │
   │                    │─────────────────>│             │
   │                    │  tool_call:      │             │
   │                    │  CALL_CONTACT    │             │
   │                    │<────────────────│             │
   │                    │                  │             │
   │                    │  push_command()  │             │
   │                    │─────────────────────────────────>│
   │                    │                  │             │
   │                    │  poll get_result()│             │
   │                    │  (async loop)     │             │
   │                    │<─────────────────────────────────│
   │                    │  {status: success,              │
   │                    │   data: {number: "+91..."}}     │
   │                    │                  │             │
   │                    │  call_groq()     │             │
   │                    │  (with tool_result)              │
   │                    │─────────────────>│             │
   │                    │  text: "Dialer    │             │
   │                    │  open ho gaya"    │             │
   │                    │<────────────────│             │
   │  {type: "message", │                  │             │
   │   response: "Dialer│                  │             │
   │   open ho gaya..."} │                  │             │
   │<───────────────────│                  │             │
```

---

## 8. Backwards Compatibility Checklist

| Feature | Before | After | Compatible? |
|---|---|---|---|
| `POST /chat` request body | `{message, conversation_id?, user_name?}` | Same | ✅ |
| `POST /chat` response — message type | `{type: "message", response, conversation_id}` | Same + `agent_steps?` | ✅ (additive) |
| `POST /chat` response — tool_call type | `{type: "tool_call", intent, parameters, requires_confirmation}` | Same + `run_id?` | ✅ (additive) |
| `POST /chat` response — android_tool_call type | `{type: "android_tool_call", intent, ...}` | Same (or handled internally by agent loop) | ✅ |
| `POST /vision` | Unchanged | Unchanged | ✅ |
| `GET /weather` | Unchanged | Unchanged | ✅ |
| `GET /tools` | Returns browser tools | Returns all tools (unified) | ✅ (additive) |
| `GET /device/*` | Unchanged | Unchanged | ✅ |
| `POST /auth/me` | Unchanged | Unchanged | ✅ |
| Frontend auth flow | Unchanged | Unchanged | ✅ |
| Voice input/output | Unchanged | Unchanged | ✅ |
| Image/vision | Unchanged | Unchanged | ✅ |
| Rate limiting | Unchanged + `/agent` bucket | ✅ |
| Supabase schema | Unchanged | Unchanged | ✅ |
| Android app polling | Unchanged | Unchanged | ✅ |
| Mock device | Unchanged | Unchanged | ✅ |

---

## 9. Key Design Decisions

### 9.1 Why Backend Agent Loop (Not Frontend)

The agent loop runs on the backend because:
- **Groq API key** must never be exposed to the frontend.
- **Multi-step LLM calls** are expensive — the backend controls step/timeout limits.
- **Android tool dispatch** is already backend-mediated — the frontend can't call the device directly.
- **Server-side tools** (Phase 2) execute on the backend.
- **Conversation history** (from Supabase) is on the backend.

The frontend's role is: send message → display responses → execute browser tools → report results back.

### 9.2 Why Stateful Run (Not Single Request)

Browser tools require frontend execution + confirmation. This means the agent loop cannot complete in a single HTTP request when:
- A tool needs confirmation (user must click "Yes").
- A tool executes in the browser (`window.open()`, `fetch /weather`).

The `run_id` + `/agent/continue` pattern lets the loop pause (return to frontend) and resume (frontend calls back with result). The state lives in-memory on the backend with a 5-minute TTL.

### 9.3 Why Not WebSocket

WebSocket would simplify the loop (no need for `/agent/continue`), but:
- Adds complexity to the FastAPI backend (ws route, connection management).
- Breaks the existing request-response model that the frontend uses.
- Requires changes to `auth.js` (token in ws connection vs. header).
- Rate limiting is harder with WebSocket.

The `/agent/continue` pattern is simpler, works with existing CORS/auth/rate-limit middleware, and is sufficient for 5-step loops with 30s timeouts.

### 9.4 Why Not Rewrite tools.py and android_tools.py

They work. They have well-tested Pydantic models. The unified registry wraps them without modifying them. If the agent loop needs to be reverted, the old code path is intact.

### 9.5 History Injection

Currently `/chat` is stateless — each call sends only the current user message to Groq. The agent loop should inject recent conversation history (last 10-20 messages from Supabase) into the LLM context. This is a **new capability** enabled by the agent loop, not a breaking change:

```python
if user_id and conversation_id:
    history = get_history(conversation_id, user_id, limit=10)
    messages = history_to_groq_messages(history) + [{"role": "user", "content": user_message}]
else:
    messages = [{"role": "user", "content": user_message}]
```

This is optional and controlled by an env var `AGENT_HISTORY_ENABLED=true` (default: true for logged-in users, false for anonymous).

---

## 10. Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Agent loop consumes too many Groq calls | Medium | Rate limit exhaustion | MAX_STEPS=5 hard limit + existing rate limiter |
| In-memory run state grows unbounded | Low | Memory leak | TTL cleanup (5 min) + max 100 concurrent runs |
| Frontend doesn't call `/agent/continue` | Medium | Orphaned run state | TTL auto-expiry; frontend shows timeout message |
| Android device slow to respond | Medium | Loop timeout | TOOL_TIMEOUT_S=15s; LLM gets error and informs user |
| LLM hallucinates tool names | Low | Validation failure | Existing Pydantic validation + ALL_TOOL_NAMES check |
| LLM calls same tool repeatedly | Medium | Infinite loop | MAX_STEPS=5 breaks the loop |
| Confirmation modal blocks loop | Low | UX confusion | Modal has "No" button → feeds "cancelled" to LLM |
| Existing single-tool requests break | Low | Regression | `/chat` response shape is backwards-compatible |
