"""
Nova AI Assistant — main.py  (v5)

Changes from v4:
- Add: auth_router mounted (/auth/me).
- Add: Authorization header allowed in CORS.
- Add: optional auth dependency on /chat (logs user id if present,
       returns 401 only when SUPABASE_JWT_SECRET is set AND token is invalid).
- Add: controlled agent loop (run_agent_loop) around Groq + tools —
       max tool iterations, wall-clock timeout, tool-result verification,
       and a final natural-language response. Normal chat (no tool) and
       auth/session behaviour are unchanged; /chat falls back to the
       single-shot path if the loop ever throws unexpectedly.
- All existing routes, tools, and Android companion unchanged.
"""

import base64
import json
import logging
import os
import time
from collections import defaultdict
from typing import ClassVar, Optional
from urllib.parse import quote_plus

import requests as http_requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from groq import APIConnectionError, APIStatusError, APITimeoutError, Groq
from pydantic import BaseModel

from android_router import router as android_router
from auth_router import router as auth_router
from auth_router import verify_token, verify_token_optional
from database import get_db
from memory import (
    create_conversation,
    delete_conversation,
    get_conversation,
    get_history,
    list_conversations,
    save_message,
    update_conversation_title,
)
from android_tools import (
    ANDROID_TOOL_NAMES,
    android_system_prompt_fragment,
    get_android_tool,
    groq_android_tools_schema,
)
from device_session import any_device_online
from tool_registry import (
    CHAT_TOOL_NAMES,
    all_tool_schemas,
    build_system_prompt,
    get_tool,
    groq_tool_schemas,
)
from user_memory import (
    MemoryRow,
    delete_all_memories,
    delete_memory as delete_one_memory,
    format_memories_for_prompt,
    recall_memories,
    save_memory as save_one_memory,
)

# Combined set of all valid tool names (registry browser tools + Android tools)
ALL_TOOL_NAMES = CHAT_TOOL_NAMES | ANDROID_TOOL_NAMES

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ---------------------------------------------------------------------------
# Logging — safe: never logs API keys or secrets
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("nova")

# Startup key presence check — logs only a masked preview, never the full key.
_startup_key = os.getenv("GROQ_API_KEY", "")
if not _startup_key:
    logger.error("STARTUP: GROQ_API_KEY is not set — all /chat requests will fail.")
elif len(_startup_key) < 10:
    logger.error("STARTUP: GROQ_API_KEY looks too short (%d chars) — check .env.", len(_startup_key))
else:
    _masked = _startup_key[:4] + ("*" * (len(_startup_key) - 8)) + _startup_key[-4:]
    logger.info("STARTUP: GROQ_API_KEY loaded — %s (length=%d)", _masked, len(_startup_key))
del _startup_key

# ---------------------------------------------------------------------------
# Groq model config
# PRIMARY  : openai/gpt-oss-20b    (1,000 tps, production model, free tier)
# FALLBACK : openai/gpt-oss-120b   (500 tps, higher quality, free tier fallback)
# VISION   : qwen/qwen3.8-27b      (only free-tier vision model as of Oct 2026)
#            See https://console.groq.com/docs/vision for current list.
#            Override via GROQ_VISION_MODEL in .env when Groq adds new models.
# ---------------------------------------------------------------------------
PRIMARY_MODEL  = os.getenv("GROQ_MODEL",        "openai/gpt-oss-20b")
FALLBACK_MODEL = "openai/gpt-oss-120b"
VISION_MODEL   = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")

# Build base system prompt once at startup (static tools only).
# Android fragment is appended dynamically per-request when a device is online.
SYSTEM_PROMPT = build_system_prompt()

# ---------------------------------------------------------------------------
# Agent loop config
# ---------------------------------------------------------------------------
MAX_TOOL_ITERATIONS = int(os.getenv("AGENT_MAX_STEPS", "4"))   # max LLM <-> tool back-and-forth rounds
LOOP_TIMEOUT_S      = int(os.getenv("AGENT_TIMEOUT_S", "45"))   # hard wall-clock cap for the entire loop

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Nova AI Assistant", version="4.0.0")

ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:3000",
    "http://localhost:5500",
    "http://127.0.0.1",
    "http://127.0.0.1:5500",
    "http://127.0.0.1:3000",
    "https://nava-ai-assistant.onrender.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Device-Id", "X-Device-Token", "X-Admin-Key"],
)

# Mount routers
app.include_router(auth_router)    # /auth/* — Email + Password + JWT verification
app.include_router(android_router) # /device/* — Android companion (unchanged)

# ---------------------------------------------------------------------------
# Rate limiter — stdlib only, no extra deps
#
# Tracks request counts per (IP, window).  Two tiers:
#   /chat        → CHAT_RPM   requests per minute
#   /device/*    → DEVICE_RPM requests per minute  (applies to poll spam)
#   everything else → GLOBAL_RPM
# ---------------------------------------------------------------------------
CHAT_RPM   = int(os.getenv("RATE_CHAT_RPM",   "20"))
DEVICE_RPM = int(os.getenv("RATE_DEVICE_RPM", "60"))
GLOBAL_RPM = int(os.getenv("RATE_GLOBAL_RPM", "120"))

# {ip: {window_minute: count}}
_rate_buckets: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))


def _check_rate_limit(ip: str, path: str) -> bool:
    """
    Returns True if the request is allowed, False if rate limit exceeded.
    Window = current UTC minute (integer).
    Old windows are pruned to prevent unbounded memory growth.
    """
    now_minute = int(time.time() // 60)

    if path.startswith("/chat"):
        limit = CHAT_RPM
    elif path.startswith("/device"):
        limit = DEVICE_RPM
    else:
        limit = GLOBAL_RPM

    bucket = _rate_buckets[ip]
    # Prune old windows (keep only last 2 minutes)
    for old in [k for k in bucket if k < now_minute - 1]:
        del bucket[old]

    bucket[now_minute] += 1
    return bucket[now_minute] <= limit


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    # X-Forwarded-For is set by Render/nginx; fallback to direct client
    forwarded = request.headers.get("X-Forwarded-For")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")

    if not _check_rate_limit(ip, request.url.path):
        logger.warning("Rate limit exceeded: ip=%s path=%s", ip, request.url.path)
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Thodi der baad try karo."},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
import re as _re

class ChatRequest(BaseModel):
    message: str
    # Optional: conversation to continue.  If omitted, a new conversation
    # is created (when the user is logged in and Supabase is configured).
    conversation_id: Optional[str] = None
    # Optional: first name of the logged-in user, sent by the frontend.
    # Validated strictly — only letters/spaces/hyphens, max 50 chars.
    # Never required; anonymous users simply omit it.
    user_name: Optional[str] = None
    # Optional: preferred response language. 'hindi' | 'english' | 'hinglish'
    # Defaults to 'hinglish' (existing behaviour) if omitted or unrecognised.
    language: Optional[str] = None
    # Optional: preferred response style. 'short' | 'normal' | 'detailed'
    # Defaults to 'normal' if omitted or unrecognised.
    response_style: Optional[str] = None

    _VALID_LANGUAGES: ClassVar[set] = {'hindi', 'english', 'hinglish'}
    _VALID_STYLES:    ClassVar[set] = {'short', 'normal', 'detailed'}

    @staticmethod
    def _clean_name(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        # Allow Unicode letters, spaces, hyphens, apostrophes (e.g. "O'Brien")
        if not _re.match(r"^[\w\s\-'\.]{1,50}$", v, _re.UNICODE):
            return None   # silently ignore invalid values — never crash on bad input
        return v[:50]

    def model_post_init(self, __context):
        # Apply cleaning after Pydantic constructs the model
        object.__setattr__(self, "user_name", self._clean_name(self.user_name))
        # Normalise language
        lang = (self.language or '').strip().lower()
        object.__setattr__(self, "language", lang if lang in self._VALID_LANGUAGES else None)
        # Normalise response_style
        style = (self.response_style or '').strip().lower()
        object.__setattr__(self, "response_style", style if style in self._VALID_STYLES else None)


# ---------------------------------------------------------------------------
# Groq helper — injects system prompt, extends with Android fragment if needed,
# and passes tools natively so the model can call them without JSON hacks.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# System-prompt / tools builders — shared by call_groq() and run_agent_loop()
# ---------------------------------------------------------------------------
def _build_system_prompt(
    user_name: Optional[str] = None,
    user_id:   Optional[str] = None,
    language:  Optional[str] = None,
    response_style: Optional[str] = None,
) -> str:
    """Base system prompt + optional user-name injection + Android fragment
    (appended only when a companion device is online) + user memories
    (fetched from Supabase when user_id is provided) + language/style prefs."""
    system_prompt = SYSTEM_PROMPT

    # ── Language preference ───────────────────────────────────────────────
    _LANG_DIRECTIVES = {
        "hindi":    "LANGUAGE: Always respond in pure Hindi (Devanagari script or Roman Hindi). Do not use English words unless there is no Hindi equivalent.",
        "english":  "LANGUAGE: Always respond in clear, natural English only. Do not mix Hindi or Hinglish.",
        "hinglish": "LANGUAGE: Respond in Hinglish — a natural mix of Hindi and English, using whichever word or phrase sounds most natural at each point. This is the default.",
    }
    if language and language in _LANG_DIRECTIVES:
        system_prompt += f"\n\n{_LANG_DIRECTIVES[language]}"

    # ── Response style ────────────────────────────────────────────────────
    _STYLE_DIRECTIVES = {
        "short":    "RESPONSE STYLE: Keep replies concise — 1–3 sentences maximum. Get straight to the point.",
        "normal":   "",   # default — no extra instruction needed
        "detailed": "RESPONSE STYLE: Give detailed, thorough replies. Explain context, provide examples, and use headings or bullet points where helpful.",
    }
    style_directive = _STYLE_DIRECTIVES.get(response_style or "normal", "")
    if style_directive:
        system_prompt += f"\n\n{style_directive}"

    if user_name:
        system_prompt += (
            f"\n\nCURRENT USER: The user's first name is {user_name}. "
            "Use their name sparingly — roughly once every 3–5 messages, "
            "and only when it feels genuinely warm (e.g. at the start of a greeting, "
            "or when reassuring them). Never use the name in every reply. "
            "Never start every sentence with their name. "
            "Never use the name more than once in a single reply. "
            "Good examples: 'Sure, {name}!', 'Got it, {name}.', 'Of course, {name} — let me check.' "
            "Bad examples: '{name}, I will {name} do this for you, {name}.' "
            "When in doubt, skip the name entirely — a warm tone matters more than name frequency."
        ).replace("{name}", user_name)

    # Inject remembered facts/preferences so Nova can use them naturally.
    if user_id:
        memories = recall_memories(user_id)
        memory_block = format_memories_for_prompt(memories)
        if memory_block:
            system_prompt += f"\n\n{memory_block}"

    if any_device_online():
        system_prompt = system_prompt + android_system_prompt_fragment()
    return system_prompt


def _build_tools() -> list[dict]:
    """Groq tool schemas for this request: browser tools + Android tools
    (Android tools only when a companion device is online)."""
    tools = groq_tool_schemas()
    if any_device_online():
        tools = tools + groq_android_tools_schema()
    return tools


def call_groq(client: Groq, model: str, user_message: str, user_name: Optional[str] = None, user_id: Optional[str] = None, language: Optional[str] = None, response_style: Optional[str] = None) -> dict:
    """
    Call Groq with native tool calling.

    user_name — if provided, appended to the system prompt so Nova can
                address the user naturally.  Sanitised before use.
    user_id   — if provided, used to inject the user's stored memories
                into the system prompt.
    language  — 'hindi' | 'english' | 'hinglish' (default: hinglish)
    response_style — 'short' | 'normal' | 'detailed' (default: normal)

    Returns one of:
      {"kind": "text",      "content": "..."}
      {"kind": "tool_call", "name": "TOOL_NAME", "arguments": {...}}
    """
    system_prompt = _build_system_prompt(user_name, user_id=user_id, language=language, response_style=response_style)
    tools = _build_tools()

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ],
        tools=tools,
        tool_choice="auto",
        temperature=0.2,
        max_tokens=1200,
        timeout=25,
    )

    choice = completion.choices[0]
    msg = choice.message

    # Native tool call — model filled in tool_calls
    if msg.tool_calls:
        tc = msg.tool_calls[0]  # Nova only dispatches one tool at a time
        try:
            arguments = json.loads(tc.function.arguments)
        except json.JSONDecodeError:
            arguments = {}
        return {"kind": "tool_call", "name": tc.function.name, "arguments": arguments}

    # Plain text reply
    return {"kind": "text", "content": (msg.content or "").strip()}


# ---------------------------------------------------------------------------
# Tool-call validator & router
# Handles BOTH browser tools (tools.py) and Android tools (android_tools.py).
# Input is now the structured dict from call_groq(), not raw LLM text.
# ---------------------------------------------------------------------------

def parse_llm_response(groq_result: dict) -> dict:
    """
    Convert the structured output from call_groq() into a frontend-ready dict.

    Input shapes (from call_groq):
      {"kind": "text",      "content": "..."}
      {"kind": "tool_call", "name": "TOOL_NAME", "arguments": {...}}

    Output shapes (to frontend):
      {"type": "message",          "response": "..."}
      {"type": "tool_call",        "intent": "...", "parameters": {...}, "requires_confirmation": bool}
      {"type": "android_tool_call","intent": "...", "parameters": {...}, "requires_confirmation": bool}
    """
    # Plain text — just pass through
    if groq_result["kind"] == "text":
        return {"type": "message", "response": groq_result["content"]}

    # Native tool call
    intent = groq_result.get("name", "")
    params = groq_result.get("arguments", {})

    if not isinstance(intent, str) or intent not in ALL_TOOL_NAMES:
        logger.warning("Model returned unknown tool name: %r", intent)
        return {"type": "message", "response": "Yeh tool abhi available nahi hai."}

    if not isinstance(params, dict):
        logger.warning("Model tool arguments not a dict: %r", params)
        return {"type": "message", "response": "Tool parameters galat format mein hain."}

    # Route to correct registry
    is_android = intent in ANDROID_TOOL_NAMES

    if is_android:
        if not any_device_online():
            logger.info("Android tool %r requested but no device online", intent)
            return {
                "type": "message",
                "response": (
                    f"'{intent}' ke liye Nova Companion app phone par kholo. "
                    "Abhi koi Android device connected nahi hai."
                ),
            }
        tool = get_android_tool(intent)
    else:
        tool = get_tool(intent)

    if tool is None:
        logger.error("Tool lookup returned None for intent=%r", intent)
        return {"type": "message", "response": "Internal tool error. Dobara try karo."}

    # Validate parameters with Pydantic
    ok, error_msg, validated = tool.validate_parameters(params)
    if not ok:
        logger.warning("Tool %s parameter validation failed: %s", intent, error_msg)
        return {
            "type": "message",
            "response": f"Parameters sahi nahi hain ({error_msg}). Dobara try karo.",
        }

    validated_params = validated.model_dump()

    # PLAY_YOUTUBE / SEARCH_YOUTUBE: attach pre-computed URL for the browser
    if intent in ("PLAY_YOUTUBE", "SEARCH_YOUTUBE") and hasattr(validated, "youtube_url"):
        validated_params["youtube_url"] = validated.youtube_url

    # Communication tools: attach pre-computed URLs for the browser
    if intent == "WHATSAPP_MESSAGE" and hasattr(validated, "whatsapp_url"):
        validated_params["whatsapp_url"] = validated.whatsapp_url
    if intent == "PHONE_CALL" and hasattr(validated, "tel_url"):
        validated_params["tel_url"] = validated.tel_url
    if intent == "SEND_EMAIL" and hasattr(validated, "mailto_url"):
        validated_params["mailto_url"] = validated.mailto_url

    logger.info("Tool validated: intent=%s android=%s", intent, is_android)

    return {
        "type": "android_tool_call" if is_android else "tool_call",
        "intent": intent,
        "parameters": validated_params,
        "requires_confirmation": tool.requires_confirmation,
    }


# ---------------------------------------------------------------------------
# Groq call with primary → fallback
# ---------------------------------------------------------------------------
def groq_with_fallback(client: Groq, user_message: str, user_name: Optional[str] = None, user_id: Optional[str] = None, language: Optional[str] = None, response_style: Optional[str] = None) -> dict:
    def _attempt(model: str) -> dict | None:
        try:
            return call_groq(client, model, user_message, user_name=user_name, user_id=user_id, language=language, response_style=response_style)
        except APIStatusError as e:
            if e.status_code == 401:
                groq_body = getattr(e, "response", None)
                try:
                    body_text = groq_body.text if groq_body is not None else "(no response body)"
                except Exception:
                    body_text = str(e)
                logger.error(
                    "Groq auth failed (401) on model=%s — Groq response: %s",
                    model,
                    body_text,
                )
                return None
            if e.status_code in (429, 500, 502, 503):
                logger.warning("Groq %d on %s — trying fallback", e.status_code, model)
                raise
            logger.error("Groq error %d on model=%s: %s", e.status_code, model, getattr(e, "message", str(e)))
            return None
        except APITimeoutError:
            logger.warning("Groq timeout on %s — trying fallback", model)
            raise
        except APIConnectionError as e:
            logger.error("Groq connection error: %s", str(e))
            return None

    # Attempt 1 — primary model
    try:
        groq_result = _attempt(PRIMARY_MODEL)
        if groq_result is None:
            return {"type": "message", "response": "API key galat hai ya expire ho gayi."}
        return parse_llm_response(groq_result)
    except (APIStatusError, APITimeoutError):
        pass

    # Attempt 2 — fallback model
    logger.info("Trying fallback: %s", FALLBACK_MODEL)
    try:
        groq_result = _attempt(FALLBACK_MODEL)
        if groq_result is None:
            return {"type": "message", "response": "Abhi thoda busy hun, dobara try karo."}
        return parse_llm_response(groq_result)
    except (APIStatusError, APITimeoutError):
        return {"type": "message", "response": "Abhi bahut zyada requests hain. 1-2 minute baad try karo."}
    except Exception as e:
        logger.exception("Unexpected fallback error: %s", str(e))
        return {"type": "message", "response": "Server mein unexpected issue hai. Thodi der baad try karo."}


# ---------------------------------------------------------------------------
# Controlled agent loop — multi-step reasoning over Groq + tools
#
# Flow (single /chat request, no extra HTTP round-trips):
#   1. Call Groq with the user message + tools.
#   2. Plain text reply         → return it (normal chat, no tool needed).
#   3. Tool call:
#      a. Verify tool name + parameters (Pydantic) — "tool result verification".
#      b. Classify:
#         - ANDROID  → can't run server-side in one request → short-circuit and
#                      return android_tool_call to the frontend (existing behaviour).
#                      Android tools are only offered when a device is online, so an
#                      offline device never reaches this branch; if it ever does, the
#                      error is fed back to the LLM instead.
#         - SERVER  → GET_WEATHER executed on the backend via _fetch_weather_data.
#         - BROWSER → PLAY_YOUTUBE / SEARCH_YOUTUBE / OPEN_WEBSITE: synthesise the
#                      target URL here; the actual window.open() happens on the
#                      frontend. The URL is fed back to the LLM as the tool result.
#      c. Feed the tool result back to Groq as a {role: "tool"} message.
#   4. Repeat up to MAX_TOOL_ITERATIONS; stop early on timeout or final reply.
#   5. Build a final natural-language response. Browser/server tool calls also
#      carry the tool action (+ tool_result for weather) so the frontend can
#      render the rich card / open the URL.
#
# Auth/session handling in /chat is untouched — this function only talks to
# Groq and the tool registry.
# ---------------------------------------------------------------------------
def _groq_completion_with_fallback(client: Groq, messages: list[dict], tools: list[dict]):
    """Single Groq completion with primary → fallback model.

    Returns (assistant_message, error_kind):
      assistant_message : the ChatCompletionMessage object, or None on failure.
      error_kind         : None on success, else one of "auth" | "busy" | "conn" | "error".
    """
    last_kind = "error"
    for model in (PRIMARY_MODEL, FALLBACK_MODEL):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.2,
                max_tokens=1500,
                timeout=25,
            )
            return completion.choices[0].message, None
        except APIStatusError as e:
            if e.status_code == 401:
                # Bad/expired key — don't bother retrying on the fallback model.
                return None, "auth"
            if e.status_code in (429, 500, 502, 503):
                last_kind = "busy"
                continue
            logger.error("Groq error %d on model=%s: %s", e.status_code, model, getattr(e, "message", str(e)))
            last_kind = "error"
            continue
        except APITimeoutError:
            last_kind = "busy"
            continue
        except APIConnectionError as e:
            logger.error("Groq connection error: %s", str(e))
            last_kind = "conn"
            continue
    return None, last_kind


_LOOP_ERROR_REPLIES = {
    "auth":  "API key galat hai ya expire ho gayi.",
    "busy":  "Abhi bahut zyada requests hain. 1-2 minute baad try karo.",
    "conn":  "Groq se connect nahi ho pa raha. Thodi der baad try karo.",
    "error": "Server mein unexpected issue hai. Thodi der baad try karo.",
}


def _execute_tool_in_loop(name: str, params: dict, user_id: Optional[str] = None) -> dict:
    """Run a browser/server tool inside the agent loop and return its result.

    The returned dict always has an "ok" key. For BROWSER tools the backend
    cannot open a tab — it synthesises the URL and describes what the frontend
    will do, which the LLM then uses to write its final reply.
    """
    if name == "GET_WEATHER":
        # Server-side execution — same helper used by the /weather endpoint.
        return _fetch_weather_data(params["city"])

    if name == "SAVE_MEMORY":
        if not user_id:
            return {"ok": False, "error": "Login required to save memories."}
        result = save_one_memory(
            user_id=user_id,
            key=params["key"],
            value=params["value"],
            source="agent",
        )
        return {"ok": result.ok, "message": result.message, "data": result.data}

    if name == "RECALL_MEMORY":
        if not user_id:
            return {"ok": False, "error": "Login required to recall memories."}
        memories = recall_memories(user_id=user_id, key=params.get("key") or None)
        if not memories:
            return {
                "ok": True,
                "memories": [],
                "message": "Koi yaad nahi hai abhi. Kuch batao jo remember karna chahte ho!",
            }
        return {
            "ok": True,
            "memories": [{"key": m.key, "value": m.value} for m in memories],
            "count": len(memories),
        }

    if name == "DELETE_MEMORY":
        if not user_id:
            return {"ok": False, "error": "Login required to delete memories."}
        key = params.get("key", "__all__")
        if key == "__all__":
            result = delete_all_memories(user_id=user_id)
        else:
            result = delete_one_memory(user_id=user_id, key=key)
        return {"ok": result.ok, "message": result.message, "data": result.data}

    if name in ("PLAY_YOUTUBE", "SEARCH_YOUTUBE"):
        url = params.get("youtube_url") or (
            f"https://www.youtube.com/results?search_query={quote_plus(params['query'])}"
        )
        return {
            "ok": True,
            "status": "ready",
            "tool": name,
            "query": params["query"],
            "url": url,
            "note": "YouTube search URL prepared; the frontend will open it in a new tab.",
        }

    if name == "OPEN_WEBSITE":
        return {
            "ok": True,
            "status": "ready",
            "tool": name,
            "url": params["url"],
            "note": "URL validated; the frontend will open it in a new tab.",
        }

    if name == "WHATSAPP_MESSAGE":
        from tool_registry import WhatsappMessageParams
        try:
            wap = WhatsappMessageParams(
                phone_number=params["phone_number"],
                message=params.get("message", ""),
            )
            url = wap.whatsapp_url
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "status": "ready",
            "tool": name,
            "phone_number": params["phone_number"],
            "message": params.get("message", ""),
            "url": url,
            "note": (
                "WhatsApp wa.me link prepared. "
                "Nova cannot send the message — the user must press Send in WhatsApp."
            ),
        }

    if name == "PHONE_CALL":
        from tool_registry import PhoneCallParams
        try:
            pc = PhoneCallParams(phone_number=params["phone_number"])
            url = pc.tel_url
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "status": "ready",
            "tool": name,
            "phone_number": params["phone_number"],
            "url": url,
            "note": (
                "tel: link prepared. "
                "Nova cannot place the call — the browser will prompt the user to confirm."
            ),
        }

    if name == "SEND_EMAIL":
        from tool_registry import SendEmailParams
        try:
            em = SendEmailParams(
                to=params["to"],
                subject=params.get("subject", ""),
                body=params.get("body", ""),
            )
            url = em.mailto_url
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {
            "ok": True,
            "status": "ready",
            "tool": name,
            "to": params["to"],
            "subject": params.get("subject", ""),
            "body": params.get("body", ""),
            "url": url,
            "note": (
                "mailto: link prepared. "
                "Nova cannot send the email — the user must click Send in their email client."
            ),
        }

    return {"ok": False, "error": f"Tool {name} cannot run inside the agent loop."}


def _finalize_agent_response(
    final_text: str,
    pending_action: Optional[dict],
    weather_result: Optional[dict],
    agent_steps: list[dict],
    last_tool_result: Optional[dict] = None,
) -> dict:
    """Build the frontend-ready response once the loop has ended.

    - If a browser/server tool was used → return a tool_call carrying the final
      NL response (+ tool_result for weather/memory, so the frontend can render
      the rich card without a second request).
    - Otherwise → return a plain message.
    """
    # Server-side tools that send their result to the frontend for rendering
    SERVER_RESULT_TOOLS = {"GET_WEATHER", "SAVE_MEMORY", "RECALL_MEMORY", "DELETE_MEMORY"}

    if pending_action is not None:
        out = {
            "type": "tool_call",
            "intent": pending_action["intent"],
            "parameters": pending_action["parameters"],
            "requires_confirmation": pending_action["requires_confirmation"],
            "response": final_text,
            "agent_steps": agent_steps,
        }
        intent = pending_action["intent"]
        if intent == "GET_WEATHER" and weather_result is not None:
            out["tool_result"] = weather_result
        elif intent in SERVER_RESULT_TOOLS and last_tool_result is not None:
            out["tool_result"] = last_tool_result
        return out
    return {"type": "message", "response": final_text, "agent_steps": agent_steps}


def run_agent_loop(
    client: Groq,
    user_message: str,
    user_name: Optional[str] = None,
    user_id:   Optional[str] = None,
    conversation_id: Optional[str] = None,
    language: Optional[str] = None,
    response_style: Optional[str] = None,
    max_iterations: int = MAX_TOOL_ITERATIONS,
    loop_timeout_s: int = LOOP_TIMEOUT_S,
    _clock=time.monotonic,
) -> dict:
    """Controlled agent loop around Groq + tools.

    Guarantees:
      - At most ``max_iterations`` LLM round-trips.
      - Hard wall-clock cap of ``loop_timeout_s`` seconds.
      - Every tool call is validated (Pydantic) and its result is checked.
      - Returns a final natural-language response.
      - Normal chat (no tool) returns immediately, identical to a single call.
      - Auth/session is handled by the caller (/chat) — not touched here.

    Returns one of:
      {"type": "message",          "response": "...", "agent_steps": [...]}
      {"type": "tool_call",        "intent":..., "parameters":..., "requires_confirmation":...,
       "response": "...", "tool_result"?: {...}, "agent_steps": [...]}
      {"type": "android_tool_call","intent":..., "parameters":..., "requires_confirmation":...,
       "agent_steps": [...]}
    """
    system_prompt = _build_system_prompt(user_name, user_id=user_id, language=language, response_style=response_style) + (
        "\n\nAGENT LOOP: You may call tools. "
        "After a tool result is returned to you, write a concise, friendly final "
        "natural-language reply to the user in Hinglish. "
        "Only call another tool if the user's request genuinely needs more than one. "
        "Never claim an action succeeded unless the tool result confirms it. "
        "For WHATSAPP_MESSAGE, PHONE_CALL, and SEND_EMAIL: if a required field is "
        "missing, reply with a plain-text question asking for it — do NOT call the tool "
        "with guessed or invented values. Once all required fields are present in the "
        "conversation history, call the tool immediately without asking again. "
        "For planning/study/roadmap requests: write a FULL structured response using "
        "markdown (## headings, **bold**, - bullets). Do NOT truncate. The token limit "
        "is high enough — use it."
    )
    tools = _build_tools()

    # ── Seed messages: system prompt + optional history + current user turn ──
    # History is fetched from the DB when the user is continuing an existing
    # conversation.  We cap at 10 turns (20 messages) to stay within context
    # limits.  Anonymous users and new conversations get no history.
    HISTORY_TURNS = int(os.getenv("AGENT_HISTORY_TURNS", "10"))  # pairs (user+bot)
    history_msgs: list[dict] = []
    if user_id and conversation_id:
        try:
            raw_history = get_history(conversation_id, user_id, limit=HISTORY_TURNS * 2)
            for h in raw_history:
                role = "user" if h.get("role") == "user" else "assistant"
                content = h.get("content", "").strip()
                if content:
                    history_msgs.append({"role": role, "content": content})
        except Exception as e:
            logger.warning("run_agent_loop: failed to load history: %s", e)

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        *history_msgs,
        {"role": "user",   "content": user_message},
    ]

    started = _clock()
    agent_steps: list[dict] = []
    pending_action:   Optional[dict] = None   # last browser/server tool action for the frontend
    weather_result:   Optional[dict] = None   # last GET_WEATHER raw data (for the rich card)
    last_tool_result: Optional[dict] = None   # last server-tool result (memory tools)

    for iteration in range(1, max_iterations + 1):
        # ── Timeout check (top of each iteration) ──────────────────────────
        if _clock() - started > loop_timeout_s:
            agent_steps.append({"step": iteration, "action": "timeout"})
            logger.warning("Agent loop timed out after %.1fs", _clock() - started)
            break

        # ── Call Groq (primary → fallback) ──────────────────────────────────
        assistant_msg, err = _groq_completion_with_fallback(client, messages, tools)
        if err is not None:
            agent_steps.append({"step": iteration, "action": "error", "error": err})
            return {
                "type": "message",
                "response": _LOOP_ERROR_REPLIES.get(err, _LOOP_ERROR_REPLIES["error"]),
                "agent_steps": agent_steps,
            }

        # ── Plain text → final reply (normal chat) ─────────────────────────
        if not assistant_msg.tool_calls:
            final_text = (assistant_msg.content or "").strip() or "Haan?"
            agent_steps.append({"step": iteration, "action": "final_reply"})
            return _finalize_agent_response(final_text, pending_action, weather_result, agent_steps, last_tool_result)

        # ── Tool call(s): validate → execute → feed back ────────────────────
        # Append the assistant message (with tool_calls) so the follow-up tool
        # result messages line up with matching ids.
        asst_dict: dict = {"role": "assistant", "content": assistant_msg.content or ""}
        call_ids: list[str] = []
        if assistant_msg.tool_calls:
            asst_dict["tool_calls"] = []
            for idx, tc in enumerate(assistant_msg.tool_calls):
                cid = tc.id or f"call_{iteration}_{idx}"
                call_ids.append(cid)
                asst_dict["tool_calls"].append({
                    "id": cid,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                })
        messages.append(asst_dict)
        short_circuit = None  # set if an Android tool is requested

        for idx, tc in enumerate(assistant_msg.tool_calls):
            cid = call_ids[idx]
            name = tc.function.name
            try:
                arguments = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                arguments = {}

            agent_steps.append({"step": iteration, "action": "tool_call", "tool": name, "params": arguments})

            # Unknown tool → feed error back so the LLM can recover.
            if name not in ALL_TOOL_NAMES:
                logger.warning("Agent loop: unknown tool %r", name)
                tool_result = {"ok": False, "error": f"Unknown tool: {name}"}
                messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(tool_result)})
                agent_steps.append({"step": iteration, "action": "tool_result", "tool": name, "ok": False})
                continue

            # ── Android tool: short-circuit to the frontend ─────────────────
            if name in ANDROID_TOOL_NAMES:
                if not any_device_online():
                    tool_result = {"ok": False, "error": "No Android device connected. Ask the user to open the Nova Companion app."}
                    messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(tool_result)})
                    agent_steps.append({"step": iteration, "action": "tool_result", "tool": name, "ok": False})
                    continue
                tool = get_android_tool(name)
                ok, error_msg, validated = tool.validate_parameters(arguments) if tool else (False, "tool not found", None)
                if not ok or validated is None:
                    tool_result = {"ok": False, "error": f"Invalid parameters: {error_msg}"}
                    messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(tool_result)})
                    agent_steps.append({"step": iteration, "action": "tool_result", "tool": name, "ok": False})
                    continue
                short_circuit = {
                    "type": "android_tool_call",
                    "intent": name,
                    "parameters": validated.model_dump(),
                    "requires_confirmation": tool.requires_confirmation,
                    "agent_steps": agent_steps,
                }
                break  # stop processing further tool_calls in this message

            # ── Browser / server tool: validate parameters ──────────────────
            tool = get_tool(name)
            if tool is None:
                tool_result = {"ok": False, "error": f"Tool lookup failed: {name}"}
                messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(tool_result)})
                agent_steps.append({"step": iteration, "action": "tool_result", "tool": name, "ok": False})
                continue

            ok, error_msg, validated = tool.validate_parameters(arguments)
            if not ok or validated is None:
                logger.warning("Agent loop: %s param validation failed: %s", name, error_msg)
                tool_result = {"ok": False, "error": f"Invalid parameters: {error_msg}"}
                messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(tool_result)})
                agent_steps.append({"step": iteration, "action": "tool_result", "tool": name, "ok": False})
                continue

            params = validated.model_dump()
            if name in ("PLAY_YOUTUBE", "SEARCH_YOUTUBE") and hasattr(validated, "youtube_url"):
                params["youtube_url"] = validated.youtube_url
            if name == "WHATSAPP_MESSAGE" and hasattr(validated, "whatsapp_url"):
                params["whatsapp_url"] = validated.whatsapp_url
            if name == "PHONE_CALL" and hasattr(validated, "tel_url"):
                params["tel_url"] = validated.tel_url
            if name == "SEND_EMAIL" and hasattr(validated, "mailto_url"):
                params["mailto_url"] = validated.mailto_url

            # Execute (server-side) or synthesise (browser) + verify the result.
            tool_result = _execute_tool_in_loop(name, params, user_id=user_id)
            if not tool_result.get("ok", True):
                logger.warning("Agent loop: %s execution not-ok: %s", name, tool_result.get("error"))
            else:
                pending_action = {
                    "intent": name,
                    "parameters": params,
                    "requires_confirmation": tool.requires_confirmation,
                }
                if name == "GET_WEATHER":
                    weather_result = tool_result
                # Track the last successful server-tool result for frontend rendering
                last_tool_result = tool_result

            agent_steps.append({"step": iteration, "action": "tool_result", "tool": name, "ok": bool(tool_result.get("ok", True))})
            messages.append({"role": "tool", "tool_call_id": cid, "content": json.dumps(tool_result)})

        # Android short-circuit → return to frontend (existing behaviour).
        if short_circuit is not None:
            return short_circuit

    # ── Max iterations reached without a final text reply ──────────────────
    logger.warning("Agent loop hit max iterations (%d)", max_iterations)
    agent_steps.append({"action": "max_iterations"})
    return _finalize_agent_response(
        "Yeh task complete karne mein zyada steps lag gaye. Thoda simplify karke dobara try karo.",
        pending_action,
        weather_result,
        agent_steps,
        last_tool_result,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def home():
    logger.info("Health check called")
    return {
        "status": "Nova AI Assistant is running",
        "version": "4.0.0",
        "model": PRIMARY_MODEL,
        "fallback_model": FALLBACK_MODEL,
        "vision_model": VISION_MODEL,
        "browser_tools": len(CHAT_TOOL_NAMES),
        "android_tools": len(ANDROID_TOOL_NAMES),
        "device_online": any_device_online(),
    }


@app.get("/tools")
def list_tools():
    return {"tools": all_tool_schemas()}


# ---------------------------------------------------------------------------
# Weather helper — shared by /weather route and agent loop
# ---------------------------------------------------------------------------
def _fetch_weather_data(city: str) -> dict:
    """
    Fetch current weather for ``city`` from wttr.in.

    This is the single implementation of weather fetching — used by both
    the ``/weather`` endpoint and the agent loop's server-side tool
    execution for ``GET_WEATHER``.

    Returns a dict with ``ok`` key:
        {"ok": True,  "city": ..., "temp_c": ...}      on success
        {"ok": False, "error": "..."}                 on any failure
    """
    from tool_registry import GetWeatherParams
    try:
        safe_city = GetWeatherParams(city=city).city
    except Exception as e:
        return {"ok": False, "error": f"City name invalid: {e}"}

    url = f"https://wttr.in/{safe_city}?format=j1"
    logger.info("Weather fetch: city=%r", safe_city)

    try:
        resp = http_requests.get(
            url, timeout=8,
            headers={"User-Agent": "Nova-AI-Assistant/4.0"},
        )
        if resp.status_code == 404:
            return {"ok": False, "error": f"City '{safe_city}' nahi mila."}
        if resp.status_code != 200:
            return {"ok": False, "error": f"Weather service unavailable (HTTP {resp.status_code})."}

        cond = resp.json()["current_condition"][0]
        return {
            "ok":           True,
            "city":         safe_city,
            "temp_c":       cond.get("temp_C", "?"),
            "temp_f":       cond.get("temp_F", "?"),
            "feels_like_c": cond.get("FeelsLikeC", "?"),
            "humidity":     cond.get("humidity", "?"),
            "description":  cond.get("weatherDesc", [{}])[0].get("value", "Unknown"),
            "wind_kmph":    cond.get("windspeedKmph", "?"),
            "visibility_km":cond.get("visibility", "?"),
        }
    except http_requests.exceptions.Timeout:
        return {"ok": False, "error": "Weather API timeout. Try again."}
    except Exception as e:
        logger.exception("Weather fetch error: %s", str(e))
        return {"ok": False, "error": "Weather data fetch mein error aaya."}


@app.get("/weather")
def get_weather(city: str = Query(..., min_length=1, max_length=100)):
    """Fetch current weather — thin wrapper around _fetch_weather_data."""
    return _fetch_weather_data(city)


class WelcomeRequest(BaseModel):
    user_name: Optional[str] = None

    @staticmethod
    def _clean_name(v: Optional[str]) -> Optional[str]:
        return ChatRequest._clean_name(v)

    def model_post_init(self, __context):
        object.__setattr__(self, "user_name", self._clean_name(self.user_name))


@app.post("/chat/welcome")
def chat_welcome(data: WelcomeRequest, user: Optional[dict] = Depends(verify_token_optional)):
    """
    Returns a personalised greeting from Nova when the user opens the app.

    When a name is known the greeting is deterministic — no Groq call needed.
    This avoids latency, rate-limit consumption, and hallucinated variations.

    Returns {"type": "message", "response": "..."} — same shape as /chat.
    """
    name = data.user_name

    # ── Deterministic path (name known) ─────────────────────────
    # Always return the exact expected string — fast, consistent, no LLM.
    if name:
        greeting = f"Welcome to Nova, {name}. Main aapki kya madad kar sakta hoon? "
        logger.info("Welcome message (deterministic) for user=%s",
                    str(user.get("sub", ""))[:8] if user else "anon")
        return {"type": "message", "response": greeting}

    # ── No name — try Groq for a warm generic greeting ───────────
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return {"type": "message", "response": "Welcome to Nova! Main aapki kya madad kar sakta hoon? "}

    client = Groq(api_key=groq_key)

    prompt = (
        "The user just opened the app. Write a short, warm welcome greeting "
        "in Hinglish (Hindi + English mix). "
        "Start with 'Welcome to Nova' and end with one helpful question like "
        "'Main aapki kya madad kar sakta hoon?' or similar. "
        "2 sentences max."
    )

    result = groq_with_fallback(client, prompt)

    # If Groq returned a tool call for some reason, fall back gracefully
    if result.get("type") != "message":
        return {"type": "message", "response": "Welcome to Nova! Main aapki kya madad kar sakta hoon? "}

    logger.info("Welcome message (Groq) for user=%s", str(user.get("sub", ""))[:8] if user else "anon")
    return result


@app.post("/vision")
async def vision(
    image: UploadFile = File(...),
    prompt: str       = Form(default="What is in this image?"),
    user_name: str    = Form(default=""),
    user: Optional[dict] = Depends(verify_token_optional),
):
    """
    Analyse an uploaded image with a text prompt using Groq's vision model.

    - Accepts: JPG / JPEG / PNG / WEBP
    - Max size: 4 MB (well under Groq's 20 MB URL limit; base64 expands ~33%)
    - Image is read into memory, base64-encoded, sent to Groq, then discarded.
      Nothing is written to disk or stored.
    - user_name is sanitised the same way as in /chat.
    - Returns: {"type": "message", "response": "..."}
    """
    # ── 1. Validate MIME type ────────────────────────────────────────────────
    ALLOWED_MIME = {
        "image/jpeg": "image/jpeg",
        "image/jpg":  "image/jpeg",
        "image/png":  "image/png",
        "image/webp": "image/webp",
    }
    content_type = (image.content_type or "").lower()
    if content_type not in ALLOWED_MIME:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "response": (
                    f"Unsupported file type '{content_type}'. "
                    "Please upload a JPG, PNG, or WEBP image."
                ),
            },
        )
    mime_type = ALLOWED_MIME[content_type]

    # ── 2. Read and size-check ───────────────────────────────────────────────
    MAX_BYTES = 4 * 1024 * 1024  # 4 MB
    image_bytes = await image.read()
    if len(image_bytes) > MAX_BYTES:
        size_mb = len(image_bytes) / (1024 * 1024)
        return JSONResponse(
            status_code=413,
            content={
                "type": "error",
                "response": (
                    f"Image too large ({size_mb:.1f} MB). "
                    "Maximum allowed size is 4 MB."
                ),
            },
        )

    if len(image_bytes) == 0:
        return JSONResponse(
            status_code=400,
            content={"type": "error", "response": "Empty image file received."},
        )

    # ── 3. Sanitise inputs ───────────────────────────────────────────────────
    safe_prompt    = prompt.strip()[:2000] if prompt.strip() else "What is in this image?"
    safe_user_name = ChatRequest._clean_name(user_name or None)

    # ── 4. Base64-encode ─────────────────────────────────────────────────────
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64}"

    # ── 5. Build system prompt (with optional name) ──────────────────────────
    vision_system = (
        "You are Nova, a helpful AI assistant. "
        "Analyse the image provided and answer the user's question clearly and concisely. "
        "Respond in the same language the user uses (English or Hinglish). "
        "Keep your reply under 200 words unless detail is truly necessary."
    )
    if safe_user_name:
        vision_system += (
            f" The user's name is {safe_user_name}. "
            "Use their name once if it feels natural."
        )

    # ── 6. Call Groq vision model ────────────────────────────────────────────
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return {"type": "message", "response": "Groq API key nahi mili. .env check karo."}

    client = Groq(api_key=groq_key)

    try:
        completion = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": safe_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                }
            ],
            temperature=0.2,
            max_tokens=512,
            timeout=30,
        )
        reply = (completion.choices[0].message.content or "").strip()
        if not reply:
            reply = "Image dekh li, lekin koi response generate nahi hua."

        logger.info(
            "Vision response: model=%s bytes=%d user=%s",
            VISION_MODEL,
            len(image_bytes),
            str(user.get("sub", ""))[:8] if user else "anon",
        )
        return {"type": "message", "response": reply}

    except APIStatusError as e:
        if e.status_code == 400:
            logger.warning("Groq vision 400: %s", getattr(e, "message", str(e)))
            return JSONResponse(
                status_code=400,
                content={
                    "type": "error",
                    "response": "Image process nahi ho saki. Please try a different image.",
                },
            )
        if e.status_code == 429:
            return JSONResponse(
                status_code=429,
                content={
                    "type": "error",
                    "response": "Groq rate limit hit. Thodi der baad try karo.",
                },
            )
        logger.error("Groq vision error %d: %s", e.status_code, getattr(e, "message", str(e)))
        return JSONResponse(
            status_code=502,
            content={"type": "error", "response": "Vision model se response nahi mila."},
        )
    except APITimeoutError:
        return JSONResponse(
            status_code=504,
            content={"type": "error", "response": "Vision request timeout ho gayi. Dobara try karo."},
        )
    except Exception as e:
        logger.exception("Vision unexpected error: %s", str(e))
        return JSONResponse(
            status_code=500,
            content={"type": "error", "response": "Unexpected error. Dobara try karo."},
        )


# ---------------------------------------------------------------------------
# /upload — PDF and TXT file upload with safe text extraction
#
# Accepted types : application/pdf, text/plain
# Max size       : 2 MB (to keep Groq context manageable)
# Files are read into memory, text is extracted, then discarded.
# Nothing is written to disk or stored permanently.
# The extracted text is injected as context into a single Groq chat call.
# API keys are never included in any response.
# ---------------------------------------------------------------------------

_UPLOAD_ALLOWED_MIME: dict[str, str] = {
    "application/pdf": "pdf",
    "text/plain":      "txt",
    # Some browsers send these for .txt
    "text/x-python":   "txt",
    "text/csv":        "txt",
}

_UPLOAD_MAX_BYTES    = 2 * 1024 * 1024  # 2 MB hard cap
_UPLOAD_MAX_CHARS    = 12_000           # characters of extracted text fed to the LLM
_UPLOAD_MAX_PROMPT   = 1_000            # characters of user prompt


def _extract_pdf_text(pdf_bytes: bytes) -> tuple[str, str | None]:
    """Extract text from a PDF using pypdf (pure-Python, in-memory).

    Returns (text, error_message).  error_message is None on success.
    The caller is responsible for bounding the extracted length.
    """
    try:
        import io
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts: list[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            parts.append(page_text)
        text = "\n".join(parts).strip()
        if not text:
            return "", "PDF mein koi readable text nahi mila (could be scanned/image-only)."
        return text, None
    except Exception as e:
        logger.warning("PDF extraction error: %s", e)
        return "", f"PDF parse nahi ho saka: {e}"


def _extract_txt_text(raw_bytes: bytes) -> tuple[str, str | None]:
    """Decode a plain-text file safely using UTF-8 with replacement for bad bytes."""
    try:
        text = raw_bytes.decode("utf-8", errors="replace").strip()
        if not text:
            return "", "File empty hai."
        return text, None
    except Exception as e:
        return "", f"Text file decode error: {e}"


@app.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    prompt: str = Form(default=""),
    user_name: str = Form(default=""),
    conversation_id: str = Form(default=""),
    language: str = Form(default=""),
    response_style: str = Form(default=""),
    user: Optional[dict] = Depends(verify_token_optional),
):
    """
    Accept a PDF or TXT file, extract its text safely, and answer the user's
    question about its content via the standard Groq chat path.

    - Accepted: application/pdf, text/plain
    - Max size: 2 MB
    - No disk writes; file bytes are discarded after extraction.
    - API keys are never echoed back in any response.
    - Returns the same {type, response, ...} shape as /chat.
    """
    # ── 1. MIME-type validation ──────────────────────────────────────────────
    content_type = (file.content_type or "").lower().split(";")[0].strip()
    if content_type not in _UPLOAD_ALLOWED_MIME:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "response": (
                    f"Unsupported file type '{content_type or 'unknown'}'. "
                    "Please upload a PDF (.pdf) or plain-text (.txt) file."
                ),
            },
        )
    file_kind = _UPLOAD_ALLOWED_MIME[content_type]

    # ── 2. Read and size-check ───────────────────────────────────────────────
    raw_bytes = await file.read()
    if len(raw_bytes) == 0:
        return JSONResponse(
            status_code=400,
            content={"type": "error", "response": "Empty file received."},
        )
    if len(raw_bytes) > _UPLOAD_MAX_BYTES:
        size_mb = len(raw_bytes) / (1024 * 1024)
        return JSONResponse(
            status_code=413,
            content={
                "type": "error",
                "response": (
                    f"File too large ({size_mb:.1f} MB). "
                    "Maximum allowed size is 2 MB."
                ),
            },
        )

    # ── 3. Extract text ──────────────────────────────────────────────────────
    if file_kind == "pdf":
        extracted_text, extract_err = _extract_pdf_text(raw_bytes)
    else:
        extracted_text, extract_err = _extract_txt_text(raw_bytes)

    del raw_bytes  # free memory immediately — we only need the text

    if extract_err:
        return JSONResponse(
            status_code=422,
            content={"type": "error", "response": extract_err},
        )

    # Truncate to keep the Groq context manageable
    if len(extracted_text) > _UPLOAD_MAX_CHARS:
        extracted_text = extracted_text[:_UPLOAD_MAX_CHARS] + "\n\n[... file truncated for context ...]"

    # ── 4. Sanitise user inputs ──────────────────────────────────────────────
    safe_prompt    = prompt.strip()[:_UPLOAD_MAX_PROMPT] if prompt.strip() else ""
    safe_user_name = ChatRequest._clean_name(user_name or None)
    safe_conv_id   = conversation_id.strip() or None
    # Validate language / response_style the same way ChatRequest does
    _lang  = (language or '').strip().lower()
    safe_language = _lang if _lang in ChatRequest._VALID_LANGUAGES else None
    _sty   = (response_style or '').strip().lower()
    safe_style = _sty if _sty in ChatRequest._VALID_STYLES else None
    filename       = (file.filename or "uploaded_file")[:120]
    # Basic filename sanitisation — only keep printable non-path characters
    import re as _re2
    filename = _re2.sub(r"[^\w\s\-\.]", "", filename)[:80] or "uploaded_file"

    # ── 5. Build the message Nova will answer ───────────────────────────────
    user_question = safe_prompt or "Please summarise this file for me."
    user_message = (
        f'The user uploaded a file named "{filename}".\n'
        f"Here is its content:\n\n"
        f"---\n{extracted_text}\n---\n\n"
        f"User's question: {user_question}"
    )

    user_id = user.get("sub", "") if user else None
    if user_id:
        logger.info(
            "Upload: kind=%s bytes_extracted=%d file=%r user=%s",
            file_kind, len(extracted_text), filename, user_id[:8],
        )
    else:
        logger.info(
            "Upload: kind=%s bytes_extracted=%d file=%r user=anonymous",
            file_kind, len(extracted_text), filename,
        )

    # ── 6. Check Groq key ───────────────────────────────────────────────────
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        return JSONResponse(
            status_code=500,
            content={"type": "error", "response": "Groq API key nahi mili. .env check karo."},
        )

    # ── 7. Run the agent loop (same path as /chat) ──────────────────────────
    client = Groq(api_key=groq_key)
    try:
        result = run_agent_loop(
            client,
            user_message,
            user_name=safe_user_name,
            user_id=user_id,
            conversation_id=safe_conv_id,
            language=safe_language,
            response_style=safe_style,
        )
    except Exception as e:
        logger.exception("Upload agent loop failed: %s", e)
        result = {"type": "message", "response": "File process karne mein unexpected error aaya. Dobara try karo."}

    # ── 8. Persist (same as /chat, fire-and-forget) ─────────────────────────
    conv_id = safe_conv_id
    if user_id:
        try:
            if not conv_id:
                conv_id = create_conversation(user_id)
                title = f"[File] {filename}"[:60]
                update_conversation_title(conv_id, user_id, title)
            save_message(conv_id, "user", f"[Uploaded file: {filename}] {safe_prompt}".strip())
            rtype = result.get("type")
            if rtype == "message":
                save_message(conv_id, "bot", result.get("response", ""))
            elif rtype == "tool_call":
                bot_text = result.get("response", "") or f"[Tool: {result.get('intent', '')}]"
                save_message(conv_id, "bot", bot_text, tool_intent=result.get("intent"))
        except Exception as e:
            logger.warning("Upload memory save failed (non-fatal): %s", e)

    result["conversation_id"] = conv_id
    return result


@app.get("/conversations")
def get_conversations(user: dict = Depends(verify_token)):
    """
    Return the logged-in user's conversation list for the sidebar.
    Requires auth — anonymous users have no history.

    Response: {"conversations": [{id, title, created_at, updated_at}, ...]}
    Ordered newest-first, max 30 items.
    """
    user_id = user.get("sub", "")
    convs = list_conversations(user_id)
    return {"conversations": convs}


@app.post("/conversations")
def new_conversation(user: dict = Depends(verify_token)):
    """
    Create a new empty conversation for the logged-in user.

    Response: {"id": "<uuid>", "title": "Nova Chat"}
    The frontend stores this ID and sends it on every subsequent /chat call.
    """
    user_id = user.get("sub", "")
    conv_id = create_conversation(user_id)
    return {"id": conv_id, "title": "Nova Chat"}


@app.get("/conversations/{conv_id}/messages")
def get_messages(conv_id: str, user: dict = Depends(verify_token)):
    """
    Return all messages for a conversation.
    Returns 403 if the conversation doesn't belong to the current user.

    Response: {"messages": [{id, role, content, created_at}, ...]}
    """
    user_id = user.get("sub", "")
    messages = get_history(conv_id, user_id)
    # get_history returns [] both when conv doesn't exist AND when it has no messages.
    # We need to distinguish ownership failure from an empty conv.
    # We do this by checking if the conv exists for this user.
    from memory import get_conversation
    conv = get_conversation(conv_id, user_id)
    if conv is None:
        return JSONResponse(
            status_code=403,
            content={"detail": "Conversation not found or access denied."},
        )
    return {"messages": messages, "conversation": conv}


@app.delete("/conversations/{conv_id}")
def remove_conversation(conv_id: str, user: dict = Depends(verify_token)):
    """
    Delete a conversation and all its messages.
    Silent success if the conversation doesn't exist or belongs to another user.

    Response: {"ok": true}
    """
    user_id = user.get("sub", "")
    delete_conversation(conv_id, user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Privacy API — /privacy/*
#
# All routes require a valid JWT (verify_token dependency).
# user_id is ALWAYS taken from the verified JWT sub — never from the request
# body or query string.  This guarantees users can only touch their own data.
#
# Routes:
#   GET    /privacy/memories          — list memories with count
#   DELETE /privacy/memories          — wipe all memories
#   DELETE /privacy/conversations     — wipe all conversations
#   DELETE /privacy/account           — full data wipe + delete auth user
# ---------------------------------------------------------------------------

@app.get("/privacy/memories")
def privacy_get_memories(user: dict = Depends(verify_token)):
    """
    Return all stored memories for the authenticated user.
    Response: {count, memories: [{key, value, source, updated_at}]}
    """
    user_id = user.get("sub", "")
    memories = recall_memories(user_id)
    return {
        "count": len(memories),
        "memories": [
            {
                "key":        m.key,
                "value":      m.value,
                "source":     m.source,
                "updated_at": m.updated_at,
            }
            for m in memories
        ],
    }


@app.delete("/privacy/memories")
def privacy_delete_memories(user: dict = Depends(verify_token)):
    """
    Wipe all memories for the authenticated user.
    Response: {ok, deleted_count, message}
    """
    user_id = user.get("sub", "")
    result = delete_all_memories(user_id)
    if not result.ok:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": result.message},
        )
    logger.info("Privacy: memories wiped user=%s", user_id[:8])
    return {
        "ok": True,
        "deleted_count": (result.data or {}).get("deleted_count", 0),
        "message": result.message,
    }


@app.delete("/privacy/conversations")
def privacy_delete_conversations(user: dict = Depends(verify_token)):
    """
    Delete ALL conversations (and their messages) for the authenticated user.
    Response: {ok, deleted_count}
    """
    user_id = user.get("sub", "")
    db = get_db()
    if db is None:
        return JSONResponse(
            status_code=503,
            content={"ok": False, "message": "Database unavailable."},
        )

    try:
        resp = (
            db.table("conversations")
            .delete()
            .eq("user_id", user_id)
            .execute()
        )
        deleted = len(resp.data) if resp.data else 0
        logger.info("Privacy: conversations wiped user=%s count=%d", user_id[:8], deleted)
        return {"ok": True, "deleted_count": deleted}
    except Exception as e:
        logger.error("privacy_delete_conversations error: %s", e)
        return JSONResponse(
            status_code=500,
            content={"ok": False, "message": "Conversations delete karne mein error aaya."},
        )


@app.delete("/privacy/account")
def privacy_delete_account(user: dict = Depends(verify_token)):
    """
    Full account deletion for the authenticated user:
      1. Delete all memories.
      2. Delete all conversations (cascade deletes messages).
      3. Delete the Supabase auth.users row via the Admin API
         (requires SUPABASE_SERVICE_ROLE_KEY in .env).

    If the service-role key is not set, steps 1 and 2 still run
    but step 3 returns a 501 — the account stays in Supabase Auth
    but all data is wiped.

    Response: {ok, message, auth_deleted: bool}
    """
    user_id = user.get("sub", "")

    errors: list[str] = []

    # ── Step 1: wipe memories ────────────────────────────────────
    mem_result = delete_all_memories(user_id)
    if not mem_result.ok:
        errors.append(f"memories: {mem_result.message}")

    # ── Step 2: wipe conversations ───────────────────────────────
    db = get_db()
    if db is not None:
        try:
            db.table("conversations").delete().eq("user_id", user_id).execute()
        except Exception as e:
            errors.append(f"conversations: {e}")

    # ── Step 3: delete Supabase auth user ────────────────────────
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    supabase_url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    auth_deleted = False

    if service_key and supabase_url:
        try:
            import requests as _http
            resp = _http.delete(
                f"{supabase_url}/auth/v1/admin/users/{user_id}",
                headers={
                    "apikey":        service_key,
                    "Authorization": f"Bearer {service_key}",
                },
                timeout=10,
            )
            if resp.status_code in (200, 204):
                auth_deleted = True
                logger.info("Privacy: auth user deleted user=%s", user_id[:8])
            else:
                err_body = resp.text[:200]
                logger.error(
                    "Privacy: auth delete failed status=%d body=%s",
                    resp.status_code, err_body,
                )
                errors.append(f"auth delete HTTP {resp.status_code}")
        except Exception as e:
            logger.error("Privacy: auth delete exception: %s", e)
            errors.append(f"auth delete error: {e}")
    else:
        logger.warning(
            "Privacy: SUPABASE_SERVICE_ROLE_KEY not set — "
            "data wiped but auth user NOT deleted for user=%s",
            user_id[:8],
        )

    if errors and not auth_deleted:
        # Partial failure — data may be inconsistent
        logger.warning("Privacy: account delete partial errors: %s", errors)

    return {
        "ok": True,
        "auth_deleted": auth_deleted,
        "message": (
            "Account aur saara data delete ho gaya."
            if auth_deleted
            else "Data delete ho gaya. Lekin Supabase account delete ke liye SUPABASE_SERVICE_ROLE_KEY set karo."
        ),
    }


@app.post("/chat")
def chat(data: ChatRequest, user: Optional[dict] = Depends(verify_token_optional)):
    user_message = data.message.strip()
    if not user_message:
        return {"type": "message", "response": "Kuch toh bolo! 😊"}

    user_id = user.get("sub", "") if user else None

    if user_id:
        logger.info(
            "Message received (length=%d chars) user=%s…",
            len(user_message),
            user_id[:8],
        )
    else:
        logger.info("Message received (length=%d chars) user=anonymous", len(user_message))

    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key:
        logger.error("GROQ_API_KEY not set")
        return {"type": "message", "response": "Groq API key nahi mili. .env check karo."}

    client = Groq(api_key=groq_key)
    # Controlled agent loop: Groq <-> tools with max iterations, wall-clock
    # timeout, tool-result verification, and a final natural-language response.
    # Falls back to the single-shot path if the loop ever throws unexpectedly,
    # so /chat can never 500 because of the loop.
    try:
        result = run_agent_loop(client, user_message, user_name=data.user_name, user_id=user_id, conversation_id=data.conversation_id, language=data.language, response_style=data.response_style)
    except Exception as e:
        logger.exception("Agent loop failed — falling back to single-shot: %s", e)
        result = groq_with_fallback(client, user_message, user_name=data.user_name, user_id=user_id, language=data.language, response_style=data.response_style)
    logger.info("Response: type=%s intent=%s", result["type"], result.get("intent", "-"))

    # ── Persist messages (only when user is logged in + Supabase configured) ──
    # This is fire-and-forget: failures are logged but never surfaced to the user.
    conv_id = data.conversation_id
    if user_id:
        try:
            # Create a new conversation if the frontend didn't send one
            if not conv_id:
                conv_id = create_conversation(user_id)
                # Use the first 60 chars of the user message as the title
                title = user_message[:60].strip()
                if title:
                    update_conversation_title(conv_id, user_id, title)

            # Save user turn
            save_message(conv_id, "user", user_message)

            # Save bot reply.
            # - message          : the final text.
            # - tool_call         : the agent loop's final natural-language response
            #                       (the action itself is rendered by the frontend).
            # - android_tool_call : no server-side NL reply yet → keep the intent tag.
            rtype = result.get("type")
            if rtype == "message":
                save_message(conv_id, "bot", result.get("response", ""))
            elif rtype == "tool_call":
                bot_text = result.get("response", "") or f"[Tool: {result.get('intent', '')}]"
                save_message(conv_id, "bot", bot_text, tool_intent=result.get("intent"))
            elif rtype == "android_tool_call":
                intent = result.get("intent", "")
                save_message(conv_id, "bot", f"[Tool: {intent}]", tool_intent=intent)

        except Exception as e:
            logger.warning("Memory save failed (non-fatal): %s", e)

    # Always return conv_id so the frontend can track the current conversation
    result["conversation_id"] = conv_id
    return result
