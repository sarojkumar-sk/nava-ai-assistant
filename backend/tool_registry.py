"""
Nova AI Assistant — tool_registry.py

Unified, modular tool registry for all Nova tools.

Design principles:
- Single source of truth for every tool Nova can invoke.
- Each tool has: name, description, category, risk level, Pydantic param
  model, and schema (for LLM + /tools endpoint).
- Risk level drives requires_confirmation: MEDIUM/HIGH → True, LOW → False.
- SEARCH_YOUTUBE and PLAY_YOUTUBE are separate tools so the LLM can
  distinguish "search for videos" from "play this song".
- ANALYZE_IMAGE is registered but NOT offered in /chat (the /vision
  endpoint handles image analysis separately).
- Android tools remain in android_tools.py — this registry covers
  browser + server tools only.

Public API:
  get_tool(name)                    → ToolDefinition | None
  validate_tool_call(name, params)  → (ok, error, tool_def, validated_model)
  all_tool_schemas()                → list[dict]   (every registered tool)
  groq_tool_schemas()               → list[dict]   (chat-only, Groq format)
  build_system_prompt()             → str          (re-exported from tools.py)

Constants:
  CHAT_TOOL_NAMES    — tools offered to the LLM in /chat
  ALL_TOOL_NAMES     — every registered tool name (for validation)
  TOOL_REGISTRY      — {name: ToolDefinition}
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Optional
from urllib.parse import quote_plus

from pydantic import BaseModel, field_validator


# ============================================================================
# System prompt (moved here from tools.py — single source of truth)
# ============================================================================

def build_system_prompt() -> str:
    """
    Build the system prompt injected into every Groq call.
    Tool descriptions are passed separately via the native tools API —
    this prompt only covers Nova's personality and conversation rules.
    """
    return """You are Nova, a helpful, friendly AI personal assistant. You speak naturally in Hinglish (Hindi + English mix) or English depending on what the user writes.

GENERAL RULES:
1. When the user's request needs a tool (open website, play YouTube, check weather, phone actions), call the appropriate tool. Do NOT describe what you are about to do — just call the tool.
2. For normal conversation (greetings, questions, general help), reply as a helpful assistant. Keep replies concise — under 3 sentences unless detail is truly needed.
3. Never claim you opened a website, sent a message, placed a call, or sent an email unless the tool was actually triggered.
4. Never invent tool names outside the tools you have been given.
5. For OPEN_WEBSITE, always use the URL the user mentioned or the most obvious one. Do not ask for confirmation in your text — the UI handles that.
6. Be warm, natural, and brief.

LONG-FORM / PLANNING REQUESTS:
When the user asks for a plan, roadmap, study guide, schedule, list of steps, or any structured content
(keywords: "plan do", "roadmap do", "guide do", "batao", "kaise karu", "steps", "schedule", "syllabus",
"prepare karo", "banana hai", "sikhna hai", "interview prep"):
- Write a full, structured response. Do NOT cut it short.
- Use this exact markdown formatting (the UI renders it):
  - `## Heading` for major sections (Day 1, Topic: React, Phase 1, etc.)
  - `**bold**` for important terms or task names
  - `- item` for bullet lists
  - `1. item` for numbered/ordered steps
  - Blank line between sections for readability
- A study/interview plan should have AT LEAST 5–7 sections covering all major topics.
- Each section should have 3–5 actionable bullet points.
- End with a brief motivational closing line.
- Do NOT apologise for length — the user explicitly asked for a plan.

TOOL SELECTION — communication tools:
- "WhatsApp", "wa", "WhatsApp pe", "WhatsApp karo"  → WHATSAPP_MESSAGE  (NOT SEND_EMAIL, NOT SEND_SMS)
- "call karo", "phone karo", "ring karo"             → PHONE_CALL        (NOT CALL_CONTACT which is Android-only)
- "email karo", "email bhejo", "mail karo"            → SEND_EMAIL        (NOT WHATSAPP_MESSAGE)
- "message karo" without "WhatsApp"                  → ask the user which app (WhatsApp, SMS, or email)

COMMUNICATION TOOL RULES (WHATSAPP_MESSAGE, PHONE_CALL, SEND_EMAIL):
These tools require specific information. If ANY required field is missing, ask for it conversationally — do NOT call the tool yet.

Required fields:
- WHATSAPP_MESSAGE: phone_number (required, digits only e.g. 919876543210), message (required — the text to send)
- PHONE_CALL:       phone_number (required)
- SEND_EMAIL:       to / email address (required), subject (ask if missing), body (ask if missing)

Extracting fields from the user's message:
- If the user includes a phone number (a sequence of 6-15 digits, optionally with +, spaces, dashes) IN their message, extract it directly — do NOT ask for it again.
- If the user includes message text after naming a number (e.g. "9876543210 pe Hello bhejo"), extract "Hello" as the message.
- If the user includes an email address (contains @) in their message, extract it — do NOT ask for it again.
- Examples:
  "WhatsApp pe 9876543210 pe Hello bhejo" → phone_number="9876543210", message="Hello" → call WHATSAPP_MESSAGE immediately
  "919876543210 par call karo"            → phone_number="919876543210" → call PHONE_CALL immediately
  "ram@gmail.com ko email karo"           → to="ram@gmail.com" → proceed to ask subject/body

Step-by-step collection rules (only when info is TRULY missing from the message):
- If phone_number is missing → ask: "Unka WhatsApp number batao (country code ke saath, e.g. 919876543210)."
- If message is missing → ask: "Kya message bhejna hai?"
- If email address is missing → ask: "Kis ko email karna hai? Email address batao."
- If subject is missing → ask: "Email ka subject kya hoga?"
- If body is missing → ask: "Email mein kya likhna hai?"
- Ask for ONE missing field at a time. Do NOT ask for multiple fields in the same message.
- Once ALL required fields are known from the conversation history, call the tool immediately — do NOT ask again.

Reading conversation history:
- If the user previously provided a phone number or email in this conversation, reuse it — do NOT ask again.
- If the user says "9876543210" after you asked for a number, treat that as the phone_number answer.
- If the user says a sentence after you asked for a message/subject/body, treat that as the answer.

Honesty rules:
- NEVER claim a WhatsApp message was sent. Nova only opens WhatsApp with a pre-filled draft — the user must press Send.
- NEVER claim a call was placed. Nova only opens a call prompt — the user must confirm.
- NEVER claim an email was sent. Nova only opens the email client with a pre-filled draft — the user must press Send.
- Always say something like "WhatsApp khul jayega, aapko Send karna hoga" in your final reply."""


# ============================================================================
# Enums
# ============================================================================

class ToolRiskLevel(str, Enum):
    """Risk classification for tools.

    LOW      → no confirmation needed (read-only, passive action)
    MEDIUM   → confirmation modal before acting (opens external resource)
    HIGH     → confirmation always required (physical-world impact)
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolCategory(str, Enum):
    """Where the tool executes."""
    BROWSER = "browser"   # executed by frontend JS (window.open, fetch)
    SERVER = "server"     # executed on the backend
    ANDROID = "android"   # executed on Android device (not in this registry)


# ============================================================================
# Pydantic parameter models
# ============================================================================

class SearchYoutubeParams(BaseModel):
    """Parameters for SEARCH_YOUTUBE — opens YouTube search results page."""
    query: str

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty.")
        if len(v) > 200:
            raise ValueError("query is too long (max 200 chars).")
        return v

    @property
    def youtube_url(self) -> str:
        return f"https://www.youtube.com/results?search_query={quote_plus(self.query)}"


class PlayYoutubeParams(BaseModel):
    """Parameters for PLAY_YOUTUBE — opens YouTube search for a song/video."""
    query: str

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty.")
        if len(v) > 200:
            raise ValueError("query is too long (max 200 chars).")
        return v

    @property
    def youtube_url(self) -> str:
        return f"https://www.youtube.com/results?search_query={quote_plus(self.query)}"


class OpenWebsiteParams(BaseModel):
    """Parameters for OPEN_WEBSITE — opens a URL in a new browser tab."""
    url: str

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        v = v.strip()
        # Auto-prepend https if scheme is missing
        if not v.startswith(("http://", "https://")):
            v = "https://" + v
        if not v.startswith(("http://", "https://")):
            raise ValueError("Only http:// and https:// URLs are allowed.")
        # Basic structure check: must have a dot in the host
        host_part = v.split("//", 1)[-1].split("/")[0]
        if "." not in host_part:
            raise ValueError(f"URL does not look valid: {v!r}")
        return v


class GetWeatherParams(BaseModel):
    """Parameters for GET_WEATHER — fetches weather for a city."""
    city: str

    @field_validator("city")
    @classmethod
    def validate_city(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("city must not be empty.")
        if len(v) > 100:
            raise ValueError("city name is too long.")
        # Allow letters, spaces, hyphens, commas — reject shell chars
        if not re.match(r"^[\w\s,\-\.]+$", v):
            raise ValueError(f"city name contains invalid characters: {v!r}")
        return v


class SaveMemoryParams(BaseModel):
    """Parameters for SAVE_MEMORY — stores one fact/preference for the user."""
    key:   str
    value: str

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        v = v.strip().lower()
        v = re.sub(r'[\s\-]+', '_', v)
        v = re.sub(r'[^a-z0-9_]', '', v)
        v = v[:80]
        if not v:
            raise ValueError("key must not be empty after normalisation.")
        if not re.match(r'^[a-z0-9_-]{1,80}$', v):
            raise ValueError("key must contain only lowercase letters, digits, underscores.")
        return v

    @field_validator("value")
    @classmethod
    def validate_value(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("value must not be empty.")
        if len(v) > 500:
            raise ValueError("value too long (max 500 chars).")
        return v


class RecallMemoryParams(BaseModel):
    """Parameters for RECALL_MEMORY — retrieves stored facts/preferences."""
    key: Optional[str] = None   # None = return all; specific key = return one

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().lower()
        v = re.sub(r'[\s\-]+', '_', v)
        v = re.sub(r'[^a-z0-9_]', '', v)
        v = v[:80]
        return v if v else None


class DeleteMemoryParams(BaseModel):
    """
    Parameters for DELETE_MEMORY.

    key = specific slug  → delete that one memory
    key = '__all__'      → wipe all memories for the user (existing conversation-
                           clear behaviour is now handled by the frontend directly)
    confirm = True       → required; the UI confirmation modal sets this
    """
    key:     str  = "__all__"   # default: wipe all (matches old behaviour)
    confirm: bool = True

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        v = v.strip()
        if v == "__all__":
            return v
        # Normalise the key slug
        norm = v.lower()
        norm = re.sub(r'[\s\-]+', '_', norm)
        norm = re.sub(r'[^a-z0-9_]', '', norm)
        norm = norm[:80]
        if not norm:
            raise ValueError("key must not be empty.")
        return norm

    @field_validator("confirm")
    @classmethod
    def validate_confirm(cls, v: bool) -> bool:
        if not v:
            raise ValueError("confirm must be true to delete memory.")
        return v


class WhatsappMessageParams(BaseModel):
    """Parameters for WHATSAPP_MESSAGE — opens wa.me link with pre-filled message."""
    phone_number: str
    message: str  # required — Nova must ask before calling the tool

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        v = v.strip()
        # Strip common formatting characters
        cleaned = re.sub(r"[\s\-\(\)\+]", "", v)
        if not re.match(r"^\d{6,15}$", cleaned):
            raise ValueError(
                f"phone_number looks invalid: {v!r}. "
                "Must be digits only (6–15), optionally with spaces/dashes/+."
            )
        return cleaned  # store digits-only; + is added when building the URL

    @field_validator("message")
    @classmethod
    def validate_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be empty. Ask the user what they want to say.")
        if len(v) > 4096:
            raise ValueError("message too long (max 4096 chars).")
        return v

    @property
    def whatsapp_url(self) -> str:
        from urllib.parse import quote as _quote
        base = f"https://wa.me/{self.phone_number}"
        if self.message:
            base += f"?text={_quote(self.message)}"
        return base


class PhoneCallParams(BaseModel):
    """Parameters for PHONE_CALL — opens a tel: link so the browser/OS dials."""
    phone_number: str

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        v = v.strip()
        # Allow optional leading + for international format
        cleaned = re.sub(r"[\s\-\(\)]", "", v)
        if not re.match(r"^\+?\d{6,15}$", cleaned):
            raise ValueError(
                f"phone_number looks invalid: {v!r}. "
                "Must be 6–15 digits, optionally starting with +."
            )
        return cleaned

    @property
    def tel_url(self) -> str:
        return f"tel:{self.phone_number}"


class SendEmailParams(BaseModel):
    """Parameters for SEND_EMAIL — opens a mailto: link with pre-filled fields."""
    to: str
    subject: str = ""
    body: str = ""

    @field_validator("to")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        # Minimal RFC-5321-style check: local@domain.tld
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError(f"email address looks invalid: {v!r}")
        if len(v) > 254:
            raise ValueError("email address too long.")
        return v

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, v: str) -> str:
        return v.strip()[:998]  # RFC 5321 line-length limit

    @field_validator("body")
    @classmethod
    def validate_body(cls, v: str) -> str:
        return v.strip()[:8000]  # generous limit; mailto URLs are truncated by OS

    @property
    def mailto_url(self) -> str:
        from urllib.parse import quote as _quote
        parts = []
        if self.subject:
            parts.append(f"subject={_quote(self.subject)}")
        if self.body:
            parts.append(f"body={_quote(self.body)}")
        qs = "&".join(parts)
        return f"mailto:{self.to}{'?' + qs if qs else ''}"


class AnalyzeImageParams(BaseModel):
    """Parameters for ANALYZE_IMAGE — server-side image analysis.

    Not offered in /chat.  The /vision endpoint handles image upload + analysis
    directly.  This model exists for schema completeness and future agent-loop
    integration.
    """
    prompt: str = "What is in this image?"

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v: str) -> str:
        v = v.strip()
        if not v:
            return "What is in this image?"
        if len(v) > 2000:
            v = v[:2000]
        return v


# ============================================================================
# ToolDefinition
# ============================================================================

class ToolDefinition:
    """Immutable description of a single tool.

    requires_confirmation is derived from risk level:
      LOW    → False
      MEDIUM → True
      HIGH   → True
    """

    def __init__(
        self,
        name: str,
        description: str,
        category: ToolCategory,
        risk: ToolRiskLevel,
        parameters_schema: dict,
        param_model: type[BaseModel],
        available_in_chat: bool = True,
    ):
        self.name = name
        self.description = description
        self.category = category
        self.risk = risk
        self.parameters_schema = parameters_schema
        self.param_model = param_model
        self.available_in_chat = available_in_chat
        self.requires_confirmation = risk in (ToolRiskLevel.MEDIUM, ToolRiskLevel.HIGH)

    def validate_parameters(self, params: dict[str, Any]) -> tuple[bool, str, BaseModel | None]:
        """Validate raw param dict against this tool's Pydantic model.

        Returns: (ok: bool, error_message: str, validated_model | None)
        """
        try:
            validated = self.param_model(**params)
            return True, "", validated
        except Exception as e:
            return False, str(e), None

    def to_dict(self) -> dict:
        """Full schema dict for /tools endpoint and documentation."""
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category.value,
            "risk": self.risk.value,
            "parameters": self.parameters_schema,
            "requires_confirmation": self.requires_confirmation,
            "available_in_chat": self.available_in_chat,
        }


# ============================================================================
# Registry
# ============================================================================

TOOL_REGISTRY: dict[str, ToolDefinition] = {

    "SEARCH_YOUTUBE": ToolDefinition(
        name="SEARCH_YOUTUBE",
        description=(
            "Search YouTube for videos matching a query and open the search results page. "
            "Use when the user asks to search for videos, find a channel, or look up content on YouTube. "
            "Does NOT play a specific video — it opens the search results page."
        ),
        category=ToolCategory.BROWSER,
        risk=ToolRiskLevel.LOW,
        parameters_schema={
            "query": {
                "type": "string",
                "description": "Search query for YouTube (e.g. 'latest tech news', 'cooking tutorials').",
            }
        },
        param_model=SearchYoutubeParams,
        available_in_chat=True,
    ),

    "PLAY_YOUTUBE": ToolDefinition(
        name="PLAY_YOUTUBE",
        description=(
            "Search YouTube for a song, video, or query and opens the results page. "
            "Use for music requests, video requests, or when the user says 'play' something. "
            "Does NOT claim music is playing — it opens the YouTube search page."
        ),
        category=ToolCategory.BROWSER,
        risk=ToolRiskLevel.LOW,
        parameters_schema={
            "query": {
                "type": "string",
                "description": "Search query for YouTube (e.g. 'Arijit Singh sad songs').",
            }
        },
        param_model=PlayYoutubeParams,
        available_in_chat=True,
    ),

    "OPEN_WEBSITE": ToolDefinition(
        name="OPEN_WEBSITE",
        description=(
            "Opens a website URL in the user's browser. "
            "Use when the user explicitly asks to open or visit a website."
        ),
        category=ToolCategory.BROWSER,
        risk=ToolRiskLevel.MEDIUM,
        parameters_schema={
            "url": {
                "type": "string",
                "description": "Full URL to open (e.g. 'https://google.com'). "
                               "If scheme is missing, https:// is assumed.",
            }
        },
        param_model=OpenWebsiteParams,
        available_in_chat=True,
    ),

    "GET_WEATHER": ToolDefinition(
        name="GET_WEATHER",
        description=(
            "Gets the current weather for a city using a public weather API. "
            "Use when the user asks about the weather, temperature, or forecast."
        ),
        category=ToolCategory.BROWSER,
        risk=ToolRiskLevel.LOW,
        parameters_schema={
            "city": {
                "type": "string",
                "description": "City name (e.g. 'Mumbai', 'Delhi', 'New York').",
            }
        },
        param_model=GetWeatherParams,
        available_in_chat=True,
    ),

    "SAVE_MEMORY": ToolDefinition(
        name="SAVE_MEMORY",
        description=(
            "Save an important fact or preference the user wants Nova to remember. "
            "Use when the user says 'remember that…', 'note that…', or shares a clear "
            "personal fact or preference (e.g. favourite color, home city, dietary preference). "
            "key: short slug (e.g. 'favourite_color', 'home_city'). "
            "value: the fact itself (e.g. 'blue', 'Mumbai'). "
            "Do NOT save passwords, tokens, API keys, or any secrets."
        ),
        category=ToolCategory.SERVER,
        risk=ToolRiskLevel.LOW,
        parameters_schema={
            "key": {
                "type": "string",
                "description": (
                    "Short lowercase slug for the fact, e.g. 'favourite_color', 'home_city'. "
                    "Use underscores. Max 80 chars."
                ),
            },
            "value": {
                "type": "string",
                "description": "The fact or preference to remember. Max 500 chars. Never store secrets.",
            },
        },
        param_model=SaveMemoryParams,
        available_in_chat=True,
    ),

    "RECALL_MEMORY": ToolDefinition(
        name="RECALL_MEMORY",
        description=(
            "Retrieve facts or preferences Nova has been told to remember. "
            "Use when the user asks 'what do you know about me?', 'do you remember…', "
            "or when a remembered fact would help answer the current question. "
            "Leave key empty to get all memories. Provide key to get one specific fact."
        ),
        category=ToolCategory.SERVER,
        risk=ToolRiskLevel.LOW,
        parameters_schema={
            "key": {
                "type": "string",
                "description": (
                    "Optional. Specific memory key to retrieve (e.g. 'home_city'). "
                    "Omit or leave empty to retrieve all memories."
                ),
            },
        },
        param_model=RecallMemoryParams,
        available_in_chat=True,
    ),

    "DELETE_MEMORY": ToolDefinition(
        name="DELETE_MEMORY",
        description=(
            "Delete a specific remembered fact (by key) or ALL memories. "
            "Use when the user says 'forget that…', 'delete my memory of…', or "
            "'clear all your memories about me'. "
            "key: specific slug to delete one fact, or '__all__' to wipe everything. "
            "Requires confirmation — always high-risk."
        ),
        category=ToolCategory.SERVER,
        risk=ToolRiskLevel.HIGH,
        parameters_schema={
            "key": {
                "type": "string",
                "description": (
                    "Memory key to delete (e.g. 'home_city'), or '__all__' to delete everything."
                ),
            },
            "confirm": {
                "type": "boolean",
                "description": "Must be true. Confirms the user wants to delete memory.",
            },
        },
        param_model=DeleteMemoryParams,
        available_in_chat=True,
    ),

    "WHATSAPP_MESSAGE": ToolDefinition(
        name="WHATSAPP_MESSAGE",
        description=(
            "Open WhatsApp Web / app with a pre-filled message to a phone number "
            "using the wa.me link. "
            "Use when the user asks to WhatsApp or message someone on WhatsApp. "
            "IMPORTANT: Nova CANNOT send the message automatically — the user must "
            "press Send inside WhatsApp. "
            "BEFORE calling this tool, you MUST have BOTH phone_number AND message. "
            "If phone_number is missing → ask the user: 'Unka WhatsApp number batao (country code ke saath).' "
            "If message is missing → ask the user: 'Kya message bhejna hai?' "
            "Ask for ONE field at a time. Do NOT call this tool until both are known. "
            "Do NOT invent or guess phone numbers."
        ),
        category=ToolCategory.BROWSER,
        risk=ToolRiskLevel.MEDIUM,
        parameters_schema={
            "phone_number": {
                "type": "string",
                "description": (
                    "Recipient phone number in international format, digits only "
                    "(e.g. '919876543210' for +91 India). "
                    "Do NOT guess — ask the user if unknown."
                ),
            },
            "message": {
                "type": "string",
                "description": (
                    "Text to pre-fill in WhatsApp. "
                    "Ask the user for the message text if not provided."
                ),
            },
        },
        param_model=WhatsappMessageParams,
        available_in_chat=True,
    ),

    "PHONE_CALL": ToolDefinition(
        name="PHONE_CALL",
        description=(
            "Open a tel: link so the browser or OS initiates a phone call. "
            "Use when the user asks to call a phone number from the browser. "
            "IMPORTANT: Nova CANNOT place the call automatically — the browser "
            "will prompt the user to confirm. "
            "This tool accesses NO contact list — only works with an explicit phone number. "
            "BEFORE calling this tool, you MUST have phone_number. "
            "If phone_number is missing → ask the user: 'Kis number par call karna hai?' "
            "Do NOT use CALL_CONTACT (that is Android-only). "
            "Do NOT invent or guess phone numbers."
        ),
        category=ToolCategory.BROWSER,
        risk=ToolRiskLevel.MEDIUM,
        parameters_schema={
            "phone_number": {
                "type": "string",
                "description": (
                    "Phone number to dial, digits only, optionally starting with +. "
                    "Do NOT guess — ask the user if unknown."
                ),
            },
        },
        param_model=PhoneCallParams,
        available_in_chat=True,
    ),

    "SEND_EMAIL": ToolDefinition(
        name="SEND_EMAIL",
        description=(
            "Open the user's default email client with a pre-filled mailto: link. "
            "Use ONLY when the user explicitly says 'email', 'mail', or similar. "
            "Do NOT use this for WhatsApp messages or SMS — use WHATSAPP_MESSAGE or SEND_SMS instead. "
            "IMPORTANT: Nova CANNOT send the email automatically — the user must "
            "click Send in their email client. "
            "BEFORE calling this tool, collect fields step-by-step: "
            "1. If 'to' (email address) is missing → ask: 'Kis ko email karna hai? Email address batao.' "
            "2. If subject is missing → ask: 'Email ka subject kya hoga?' "
            "3. If body is missing → ask: 'Email mein kya likhna hai?' "
            "Ask for ONE field at a time. Do NOT call this tool until 'to' is known. "
            "Subject and body are optional — if the user says 'bas email kholo' or similar, call with only 'to'. "
            "Do NOT invent email addresses."
        ),
        category=ToolCategory.BROWSER,
        risk=ToolRiskLevel.MEDIUM,
        parameters_schema={
            "to": {
                "type": "string",
                "description": (
                    "Recipient email address (e.g. 'ram@example.com'). "
                    "Do NOT guess — ask the user if unknown."
                ),
            },
            "subject": {
                "type": "string",
                "description": "Email subject line. Optional — leave empty if not specified.",
            },
            "body": {
                "type": "string",
                "description": "Email body text. Optional — leave empty if not specified.",
            },
        },
        param_model=SendEmailParams,
        available_in_chat=True,
    ),

    "ANALYZE_IMAGE": ToolDefinition(
        name="ANALYZE_IMAGE",
        description=(
            "Analyse an uploaded image and answer a question about it. "
            "This tool is NOT available in regular chat — use the /vision endpoint "
            "to upload an image with a text prompt."
        ),
        category=ToolCategory.SERVER,
        risk=ToolRiskLevel.LOW,
        parameters_schema={
            "prompt": {
                "type": "string",
                "description": "Question or instruction about the image (default: 'What is in this image?').",
            }
        },
        param_model=AnalyzeImageParams,
        # Not offered to the LLM in /chat — the /vision endpoint handles this.
        available_in_chat=False,
    ),
}

# ============================================================================
# Name sets
# ============================================================================

# Tools offered to the LLM in /chat (excludes ANALYZE_IMAGE)
CHAT_TOOL_NAMES: set[str] = {
    name for name, t in TOOL_REGISTRY.items() if t.available_in_chat
}

# All registered tool names (for validation — includes ANALYZE_IMAGE)
ALL_TOOL_NAMES: set[str] = set(TOOL_REGISTRY.keys())


# ============================================================================
# Public helpers
# ============================================================================

def get_tool(name: str) -> ToolDefinition | None:
    """Look up a tool by name. Returns None if not found."""
    return TOOL_REGISTRY.get(name)


def validate_tool_call(
    name: str,
    params: dict,
) -> tuple[bool, str, ToolDefinition | None, BaseModel | None]:
    """
    Validate a tool call end-to-end: check name exists, then validate params.

    Returns:
        (ok, error_message, tool_definition, validated_model)
        - ok=False, tool=None  → unknown tool name
        - ok=False, tool=def   → parameter validation failed
        - ok=True              → everything valid
    """
    tool = get_tool(name)
    if tool is None:
        return False, f"Unknown tool: {name!r}", None, None

    ok, error_msg, validated = tool.validate_parameters(params)
    return ok, error_msg, tool, validated


def all_tool_schemas() -> list[dict]:
    """Return schema for ALL registered tools (for /tools endpoint)."""
    return [t.to_dict() for t in TOOL_REGISTRY.values()]


def groq_tool_schemas(chat_only: bool = True) -> list[dict]:
    """
    Return tool definitions in OpenAI/Groq native function-calling format.
    Passed directly to client.chat.completions.create(tools=...).

    Required vs optional fields are determined by the Pydantic model's
    field metadata — NOT by the static ``parameters_schema`` dict.
    A field is marked required only if it has no default and is not Optional.

    Args:
        chat_only: If True, only return tools with available_in_chat=True.
                   If False, return all tools (for future agent-loop use).
    """
    result = []
    for t in TOOL_REGISTRY.values():
        if chat_only and not t.available_in_chat:
            continue

        properties = {}
        required = []
        for param_name, param_info in t.parameters_schema.items():
            properties[param_name] = {
                "type": param_info["type"],
                "description": param_info["description"],
            }

            # Check Pydantic model for whether this field is required.
            # A field is required if it has no default value and is not Optional.
            field_info = t.param_model.model_fields.get(param_name)
            if field_info is not None:
                # Pydantic v2: field_info.is_required() returns True when no default
                if field_info.is_required():
                    required.append(param_name)
            else:
                # Fallback: if not in model_fields, treat as required
                required.append(param_name)

        result.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
    return result


# ============================================================================
# Public API — explicit exports
# ============================================================================

__all__ = [
    # Enums
    "ToolRiskLevel",
    "ToolCategory",
    # Param models
    "SearchYoutubeParams",
    "PlayYoutubeParams",
    "OpenWebsiteParams",
    "GetWeatherParams",
    "SaveMemoryParams",
    "RecallMemoryParams",
    "DeleteMemoryParams",
    "WhatsappMessageParams",
    "PhoneCallParams",
    "SendEmailParams",
    "AnalyzeImageParams",
    # Core
    "ToolDefinition",
    "TOOL_REGISTRY",
    "CHAT_TOOL_NAMES",
    "ALL_TOOL_NAMES",
    # Helpers
    "get_tool",
    "validate_tool_call",
    "all_tool_schemas",
    "groq_tool_schemas",
    "build_system_prompt",
]
