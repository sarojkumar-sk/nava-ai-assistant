"""
Nova AI Assistant — tools.py  (backward-compatibility shim)

All tool definitions now live in ``tool_registry.py``. This file remains
only so that any code still importing from ``tools`` (e.g. external scripts,
old commits) continues to work.

It re-exports every public name from ``tool_registry`` and also provides
back-compat aliases for the old function names:
    all_tools_schema()  →  all_tool_schemas()
    groq_tools_schema()  →  groq_tool_schemas()

New code should import from ``tool_registry`` directly.
"""

from tool_registry import *  # noqa: F401,F403
from tool_registry import (
    ALL_TOOL_NAMES,
    CHAT_TOOL_NAMES,
    TOOL_REGISTRY,
    AnalyzeImageParams,
    DeleteMemoryParams,
    GetWeatherParams,
    OpenWebsiteParams,
    PhoneCallParams,
    PlayYoutubeParams,
    RecallMemoryParams,
    SaveMemoryParams,
    SearchYoutubeParams,
    SendEmailParams,
    ToolCategory,
    ToolDefinition,
    ToolRiskLevel,
    WhatsappMessageParams,
    all_tool_schemas,
    build_system_prompt,
    get_tool,
    groq_tool_schemas,
    validate_tool_call,
)

# ── Backward-compat aliases (old plural names) ──────────────────────────
all_tools_schema = all_tool_schemas
groq_tools_schema = groq_tool_schemas
