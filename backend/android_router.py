"""
Nova AI Assistant — android_router.py  (v2)

FastAPI router for Android companion device communication.

Auth model:
  - Sensitive endpoints (register, dispatch command, list devices) require
    X-Admin-Key header matching NOVA_ADMIN_KEY env var.
    Only the device owner can register devices or send commands.
  - Device→server calls (poll, result) use X-Device-Id + X-Device-Token
    headers (token issued at registration time).

Endpoint summary:
  POST /device/register          — Register device (Admin-Key required)
  GET  /device/status            — Is any device online? (public)
  GET  /device/list              — List devices (Admin-Key required)
  GET  /device/poll              — Device polls for commands (Device auth)
  POST /device/result            — Device submits result (Device auth)
  GET  /device/result/{cmd_id}   — Web UI polls for result (public)
  POST /device/command           — Web UI dispatches command (Admin-Key required)
  GET  /device/tools             — List Android tool schemas (public)
"""

from __future__ import annotations

import hmac
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from android_tools import (
    ANDROID_TOOL_NAMES,
    all_android_tools_schema,
    get_android_tool,
)
from device_session import (
    authenticate,
    any_device_online,
    first_online_device,
    get_result,
    get_session,
    list_devices,
    pop_pending_commands,
    push_command,
    register_device,
    store_result,
)

logger = logging.getLogger("nova.android_router")
router = APIRouter(prefix="/device", tags=["Android Companion"])

# Optional Bearer extractor — used by /device/devices (user JWT, not admin key)
_bearer_optional = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _check_admin_key(provided: str) -> None:
    """
    Raise HTTP 403 if the provided key does not match NOVA_ADMIN_KEY.
    If NOVA_ADMIN_KEY is not set in env, all admin routes are locked (503).
    Key is NEVER logged.
    """
    expected = os.getenv("NOVA_ADMIN_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="NOVA_ADMIN_KEY not configured on server. Set it in .env.",
        )
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Admin-Key header.",
        )


def _check_device_auth(device_id: str, token: str):
    """
    Validate X-Device-Id + X-Device-Token.
    Returns the authenticated session or raises 401.
    """
    session = authenticate(device_id, token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device credentials.",
        )
    return session


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    device_id:   str
    device_name: str


class RegisterResponse(BaseModel):
    ok:          bool
    token:       str
    device_name: str
    message:     str


class CommandRequest(BaseModel):
    """Sent by the Web UI to dispatch an action to the device."""
    intent:     str
    parameters: dict
    device_id:  str = ""   # if empty, server picks first online device


class CommandDispatchResponse(BaseModel):
    ok:         bool
    command_id: str = ""
    message:    str


class ResultSubmission(BaseModel):
    """Sent by the Android app after executing (or failing) a command."""
    command_id: str
    status:     str   # success | cancelled | failed | permission_denied
    detail:     str = ""
    data:       dict = {}


class PollResponse(BaseModel):
    commands: list[dict]
    server_time: float


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/register", response_model=RegisterResponse)
def device_register(
    body: RegisterRequest,
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
):
    """
    Register or re-register an Android device.
    Requires X-Admin-Key — only the owner can add devices.
    Returns a fresh session token every time (re-registration invalidates old token).
    """
    _check_admin_key(x_admin_key)

    if not body.device_id or not body.device_name:
        raise HTTPException(status_code=400, detail="device_id and device_name are required.")

    try:
        session = register_device(body.device_id, body.device_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    logger.info("Device registered: id=%r name=%r", body.device_id, body.device_name)
    return RegisterResponse(
        ok=True,
        token=session.token,
        device_name=session.device_name,
        message=f"Registered successfully as '{session.device_name}'.",
    )


@router.get("/status")
def device_status():
    """
    Public: Web UI polls to check if a companion device is connected.
    Does NOT return tokens or device IDs.
    """
    online = any_device_online()
    device = first_online_device()
    return {
        "device_connected": online,
        "device_name": device.device_name if device else None,
        "android_tools_available": sorted(ANDROID_TOOL_NAMES) if online else [],
    }


@router.get("/devices")
def device_list_authenticated(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_optional),
):
    """
    Authenticated: return all registered devices with online status + last-seen.
    Requires a valid user JWT (same token the web UI uses for /chat).
    No tokens or sensitive fields are returned — only display-safe info.

    Response shape:
      {
        "devices": [
          {
            "device_id":     "<first 8 chars>…",   // truncated for display
            "device_name":   "Saroj's Phone",
            "online":        true,
            "registered_at": 1234567890.0,          // Unix timestamp
            "last_seen":     1234567891.0,
            "pending_commands": 0
          }
        ],
        "any_online": true
      }
    """
    # Require auth — return 401 if no valid token
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Verify JWT via the same helper used by main.py
    try:
        from auth_router import _decode_supabase_jwt
        _decode_supabase_jwt(credentials.credentials)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
        )

    devices = list_devices()
    # Truncate device_id so it's display-safe but not a usable credential
    safe_devices = []
    for d in devices:
        safe_devices.append({
            "device_id":        d["device_id"][:8] + "…" if len(d["device_id"]) > 8 else d["device_id"],
            "device_name":      d["device_name"],
            "online":           d["online"],
            "registered_at":    d["registered_at"],
            "last_seen":        d["last_seen"],
            "pending_commands": d["pending_commands"],
        })

    return {
        "devices":    safe_devices,
        "any_online": any_device_online(),
    }


@router.get("/list")
def device_list(x_admin_key: str = Header(default="", alias="X-Admin-Key")):
    """Admin: list registered devices. No tokens returned."""
    _check_admin_key(x_admin_key)
    return {"devices": list_devices()}


@router.get("/poll")
def device_poll(
    x_device_id:    str = Header(..., alias="X-Device-Id"),
    x_device_token: str = Header(..., alias="X-Device-Token"),
):
    """
    Android app calls this every 3–5 s to receive pending commands.
    Returns all queued commands and clears the queue (drain semantics).
    """
    session = _check_device_auth(x_device_id, x_device_token)
    commands = pop_pending_commands(x_device_id)
    logger.debug("Device %r polled — %d command(s) returned", x_device_id, len(commands))
    return PollResponse(commands=commands, server_time=time.time())


@router.post("/result")
def device_result(
    body: ResultSubmission,
    x_device_id:    str = Header(..., alias="X-Device-Id"),
    x_device_token: str = Header(..., alias="X-Device-Token"),
):
    """
    Android app submits the outcome of a command.
    status must be: success | cancelled | failed | permission_denied
    """
    _check_device_auth(x_device_id, x_device_token)

    valid_statuses = {"success", "cancelled", "failed", "permission_denied"}
    if body.status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {sorted(valid_statuses)}",
        )

    ok = store_result(
        x_device_id,
        body.command_id,
        {"status": body.status, "detail": body.detail, "data": body.data},
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Device not found.")

    logger.info(
        "Result received: device=%r cmd=%r status=%s",
        x_device_id, body.command_id, body.status,
    )
    return {"ok": True, "command_id": body.command_id}


@router.get("/result/{command_id}")
def poll_result(command_id: str, device_id: str = ""):
    """
    Web UI polls this to check if a command has completed.
    Returns {ready: false} if result not yet available.
    """
    if not device_id:
        dev = first_online_device()
        if dev is None:
            return {"ready": False, "result": None}
        device_id = dev.device_id

    result = get_result(device_id, command_id)
    if result is None:
        return {"ready": False, "result": None}
    return {"ready": True, "result": result}


@router.post("/command", response_model=CommandDispatchResponse)
def dispatch_command(
    body: CommandRequest,
    x_admin_key: str = Header(default="", alias="X-Admin-Key"),
):
    """
    Web UI dispatches an Android tool call to the device.
    Requires X-Admin-Key.

    Validates:
    1. intent is a known Android tool
    2. parameters pass Pydantic validation
    3. A device is online
    """
    _check_admin_key(x_admin_key)

    # ── 1. Validate intent ─────────────────────────────────────
    if body.intent not in ANDROID_TOOL_NAMES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown Android tool: '{body.intent}'. Valid: {sorted(ANDROID_TOOL_NAMES)}",
        )

    tool = get_android_tool(body.intent)

    # ── 2. Validate parameters ─────────────────────────────────
    ok, err_msg, validated = tool.validate_parameters(body.parameters)
    if not ok:
        raise HTTPException(
            status_code=422,
            detail=f"Parameter validation failed for {body.intent}: {err_msg}",
        )

    # ── 3. Resolve target device ───────────────────────────────
    if body.device_id:
        session = get_session(body.device_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Device not registered.")
        if not session.is_online():
            offline_secs = int(time.time() - session.last_seen)
            raise HTTPException(
                status_code=503,
                detail=f"Device '{session.device_name}' is offline ({offline_secs}s ago).",
            )
    else:
        session = first_online_device()
        if session is None:
            raise HTTPException(
                status_code=503,
                detail="No Android device connected. Open Nova Companion app on your phone.",
            )

    # ── 4. Build & enqueue command ─────────────────────────────
    command_id = str(uuid.uuid4())
    command = {
        "command_id": command_id,
        "intent":     body.intent,
        "parameters": validated.model_dump(),
        "issued_at":  time.time(),
        "timeout_s":  tool.timeout_s,
    }

    push_command(session.device_id, command)
    logger.info(
        "Command dispatched: device=%r intent=%s cmd_id=%s",
        session.device_id, body.intent, command_id,
    )

    return CommandDispatchResponse(
        ok=True,
        command_id=command_id,
        message=f"'{body.intent}' queued for '{session.device_name}' (cmd_id={command_id})",
    )


@router.get("/tools")
def android_tools_list():
    """Public: list all Android tool schemas."""
    return {"tools": all_android_tools_schema()}
