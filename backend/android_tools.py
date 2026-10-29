"""
Nova AI Assistant — android_tools.py

Android-only tool definitions.  These tools can ONLY execute when a
registered Android companion device is online.  The web browser cannot
execute these — they require real Android APIs.

JSON Command Contract
─────────────────────
Every command sent to the device has this envelope:

    {
      "command_id":  "uuid4-string",   // unique per command
      "intent":      "TOOL_NAME",      // see ANDROID_TOOL_NAMES
      "parameters":  { ... },          // validated by Pydantic model below
      "issued_at":   1234567890.123,   // Unix timestamp
      "timeout_s":   30                // device should abandon after this many s
    }

Every result back from the device has this envelope:

    {
      "command_id":  "uuid4-string",   // matches the command
      "status":      "success" | "cancelled" | "failed" | "permission_denied",
      "detail":      "human-readable description",
      "data":        { ... }           // optional: result data (e.g. contact list)
    }

Status meanings:
  success          — Android Intent was dispatched and app responded OK
  cancelled        — user dismissed the confirmation dialog or action screen
  failed           — intent failed or exception thrown on device
  permission_denied— required Android permission not granted

Limitations (documented honestly):
  - ACTION_CALL requires CALL_PHONE permission (dangerous, user must grant).
  - ACTION_DIAL opens the dialer pre-filled but does NOT place the call
    automatically — user must press the green button.  This is by design:
    making a call without user interaction is prevented by Android security.
  - SEND_SMS via Intent opens the SMS app pre-filled; the user must press Send.
    Silent SMS requires SEND_SMS permission + no UI — we use the Intent path
    for safety and transparency.
  - GET_CONTACTS requires READ_CONTACTS permission.
  - SET_ALARM uses AlarmClock Intent which requires no special permission on
    modern Android but the Clock app must be installed.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, field_validator

# ── Tool name registry ─────────────────────────────────────────
ANDROID_TOOL_NAMES = {
    "CALL_CONTACT",
    "SEND_SMS",
    "SEND_MESSAGE",   # alias for SEND_SMS — LLM may use either name
    "SET_ALARM",
    "GET_CONTACTS",
}

# ── Default command timeout ────────────────────────────────────
DEFAULT_TIMEOUT_S = 30


# =============================================================================
# Parameter models
# =============================================================================

class CallContactParams(BaseModel):
    """
    Dial a contact by name or phone number.

    Flow:
    1. Backend sends CALL_CONTACT with contact_name or phone_number.
    2. Android app searches contacts.
    3. If multiple matches → app sends back GET_CONTACTS result with matches.
    4. Web UI asks user to pick one.
    5. Web sends CALL_CONTACT again with exact phone_number.
    6. Android shows confirmation dialog, then ACTION_DIAL (or ACTION_CALL).
    """
    contact_name:  Optional[str] = None   # search by name
    phone_number:  Optional[str] = None   # direct dial if known
    use_action_call: bool = False          # True = ACTION_CALL (auto-dials, needs permission)
                                           # False = ACTION_DIAL (opens dialer, safer default)

    @field_validator("contact_name")
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        if len(v) > 100:
            raise ValueError("contact_name too long")
        if not re.match(r"^[\w\s\-\.\']+$", v):
            raise ValueError("contact_name contains invalid characters")
        return v

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = v.strip()
        # Allow digits, spaces, +, -, (, )
        cleaned = re.sub(r"[\s\-\(\)]", "", v)
        if not re.match(r"^\+?[\d]{6,15}$", cleaned):
            raise ValueError(f"phone_number looks invalid: {v!r}")
        return v

    def has_target(self) -> bool:
        return bool(self.contact_name or self.phone_number)


class SendSmsParams(BaseModel):
    """
    Open SMS app pre-filled with recipient and message body.
    The user must press Send — no silent SMS.
    """
    phone_number: str
    message_body: str

    @field_validator("phone_number")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        v = v.strip()
        cleaned = re.sub(r"[\s\-\(\)]", "", v)
        if not re.match(r"^\+?[\d]{6,15}$", cleaned):
            raise ValueError(f"phone_number looks invalid: {v!r}")
        return v

    @field_validator("message_body")
    @classmethod
    def validate_body(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message_body must not be empty")
        if len(v) > 1000:
            raise ValueError("message_body too long (max 1000 chars)")
        return v


class SetAlarmParams(BaseModel):
    """
    Set an alarm via AlarmClock Intent.
    No permission required; uses the system Clock app.
    """
    hour:   int       # 0–23
    minute: int       # 0–59
    label:  str = "Nova Alarm"
    days:   list[int] = []   # 0=Mon … 6=Sun; empty = one-time alarm

    @field_validator("hour")
    @classmethod
    def validate_hour(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("hour must be 0–23")
        return v

    @field_validator("minute")
    @classmethod
    def validate_minute(cls, v: int) -> int:
        if not 0 <= v <= 59:
            raise ValueError("minute must be 0–59")
        return v

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str) -> str:
        v = v.strip()
        if len(v) > 60:
            v = v[:60]
        return v


class GetContactsParams(BaseModel):
    """
    Search the device contacts by name.
    Returns a list of {name, phone_numbers[]} matches.
    """
    query: str

    @field_validator("query")
    @classmethod
    def validate_query(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("query must not be empty")
        if len(v) > 100:
            raise ValueError("query too long")
        return v


# =============================================================================
# Tool definition (mirrors browser tools.py but Android-specific)
# =============================================================================

class AndroidToolDefinition:
    def __init__(
        self,
        name: str,
        description: str,
        parameters_schema: dict,
        requires_confirmation: bool,
        requires_device: bool,      # always True for Android tools
        param_model: type[BaseModel],
        required_permissions: list[str],
        timeout_s: int = DEFAULT_TIMEOUT_S,
    ):
        self.name                 = name
        self.description          = description
        self.parameters_schema    = parameters_schema
        self.requires_confirmation= requires_confirmation
        self.requires_device      = requires_device
        self.param_model          = param_model
        self.required_permissions = required_permissions
        self.timeout_s            = timeout_s

    def validate_parameters(self, params: dict) -> tuple[bool, str, Optional[BaseModel]]:
        try:
            validated = self.param_model(**params)
            return True, "", validated
        except Exception as e:
            return False, str(e), None

    def to_dict(self) -> dict:
        return {
            "name":                  self.name,
            "description":           self.description,
            "parameters":            self.parameters_schema,
            "requires_confirmation": self.requires_confirmation,
            "requires_device":       self.requires_device,
            "required_permissions":  self.required_permissions,
            "timeout_s":             self.timeout_s,
        }


# =============================================================================
# Registry
# =============================================================================

ANDROID_TOOL_REGISTRY: dict[str, AndroidToolDefinition] = {

    "CALL_CONTACT": AndroidToolDefinition(
        name="CALL_CONTACT",
        description=(
            "Search device contacts by name and open the dialer (ACTION_DIAL) "
            "pre-filled with their number. User must press the green call button. "
            "Does NOT auto-call without user action."
        ),
        parameters_schema={
            "contact_name":    {"type": "string",  "description": "Name to search in contacts"},
            "phone_number":    {"type": "string",  "description": "Direct number (skip search)"},
            "use_action_call": {"type": "boolean", "description": "true = auto-dial (needs CALL_PHONE permission)"},
        },
        requires_confirmation=True,
        requires_device=True,
        param_model=CallContactParams,
        required_permissions=[
            "android.permission.READ_CONTACTS",
            # CALL_PHONE only if use_action_call=true
        ],
        timeout_s=45,
    ),

    "SEND_SMS": AndroidToolDefinition(
        name="SEND_SMS",
        description=(
            "Open the SMS app pre-filled with recipient and message. "
            "User must press Send — no silent background SMS."
        ),
        parameters_schema={
            "phone_number": {"type": "string", "description": "Recipient phone number"},
            "message_body": {"type": "string", "description": "Message text (max 1000 chars)"},
        },
        requires_confirmation=True,
        requires_device=True,
        param_model=SendSmsParams,
        required_permissions=[],   # Intent-based: no permission needed
        timeout_s=60,
    ),

    # SEND_MESSAGE is an alias for SEND_SMS — same behaviour, same risk level.
    # The LLM may choose either name; both resolve to the same Android intent.
    "SEND_MESSAGE": AndroidToolDefinition(
        name="SEND_MESSAGE",
        description=(
            "Send a message to a contact by opening the SMS app pre-filled. "
            "User must press Send — no silent background SMS. "
            "Alias for SEND_SMS."
        ),
        parameters_schema={
            "phone_number": {"type": "string", "description": "Recipient phone number"},
            "message_body": {"type": "string", "description": "Message text (max 1000 chars)"},
        },
        requires_confirmation=True,
        requires_device=True,
        param_model=SendSmsParams,
        required_permissions=[],
        timeout_s=60,
    ),

    "SET_ALARM": AndroidToolDefinition(
        name="SET_ALARM",
        description=(
            "Set an alarm on the device using the system AlarmClock Intent. "
            "No special permission required. Clock app must be installed."
        ),
        parameters_schema={
            "hour":   {"type": "integer", "description": "Hour (0–23)"},
            "minute": {"type": "integer", "description": "Minute (0–59)"},
            "label":  {"type": "string",  "description": "Alarm label"},
            "days":   {"type": "array",   "description": "Repeat days: 0=Mon … 6=Sun; [] = one-time"},
        },
        requires_confirmation=False,
        requires_device=True,
        param_model=SetAlarmParams,
        required_permissions=[],
        timeout_s=20,
    ),

    "GET_CONTACTS": AndroidToolDefinition(
        name="GET_CONTACTS",
        description=(
            "Search device contacts by name query. "
            "Returns a list of matching {name, phone_numbers} objects. "
            "Used internally by CALL_CONTACT when multiple matches exist."
        ),
        parameters_schema={
            "query": {"type": "string", "description": "Contact name search query"},
        },
        requires_confirmation=False,
        requires_device=True,
        param_model=GetContactsParams,
        required_permissions=["android.permission.READ_CONTACTS"],
        timeout_s=15,
    ),
}


def get_android_tool(name: str) -> Optional[AndroidToolDefinition]:
    return ANDROID_TOOL_REGISTRY.get(name)


def all_android_tools_schema() -> list[dict]:
    return [t.to_dict() for t in ANDROID_TOOL_REGISTRY.values()]


def groq_android_tools_schema() -> list[dict]:
    """
    Return Android tool definitions in OpenAI/Groq native function-calling format.
    Appended to groq_tools_schema() when a device is online.
    """
    result = []
    for t in ANDROID_TOOL_REGISTRY.values():
        properties = {}
        required = []
        for param_name, param_info in t.parameters_schema.items():
            prop: dict = {"description": param_info["description"]}
            ptype = param_info.get("type", "string")
            if ptype == "array":
                prop["type"] = "array"
                prop["items"] = {"type": "integer"}
            else:
                prop["type"] = ptype
            properties[param_name] = prop
            # Android tools often have optional fields; only mark clearly required ones
            if param_name not in ("contact_name", "phone_number", "use_action_call", "days"):
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


def android_system_prompt_fragment() -> str:
    """
    Returns the fragment to append to the main system prompt
    when a device is online.  Describes Android-only tools to the LLM.
    """
    lines = []
    for t in ANDROID_TOOL_REGISTRY.values():
        lines.append(
            f"- {t.name} [ANDROID]: {t.description}\n"
            f"  Parameters: {t.parameters_schema}\n"
            f"  requires_confirmation: {str(t.requires_confirmation).lower()}"
        )
    tool_docs = "\n".join(lines)

    return f"""
You also have access to these ANDROID device tools (only available when a phone is connected):
{tool_docs}

ANDROID TOOL RULES:
- Use these ONLY when a phone action is clearly requested (calls, SMS, alarms, contacts).
- For CALL_CONTACT: if contact_name is given without a phone_number, first use GET_CONTACTS
  to find matches, then present choices to the user before calling.
- Never claim a call was placed — ACTION_DIAL only opens the dialer.
- Never send SMS silently — always use the Intent path so user can review.
- If no device is connected, tell the user to connect the Nova companion app.
- The JSON structure is identical: {{"type":"tool_call","intent":"ANDROID_TOOL","parameters":{{...}},"requires_confirmation":true/false}}
"""
