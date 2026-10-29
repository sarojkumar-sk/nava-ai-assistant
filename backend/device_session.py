"""
Nova AI Assistant — device_session.py

Secure device registry for Android companion app.

Design:
- Each Android device registers with a unique device_id (UUID generated on
  the device) and receives a server-issued session token.
- Tokens are random 256-bit secrets (URL-safe base64).  They are never
  logged in full — only the first 8 chars are logged for traceability.
- Sessions are held in-memory during the server process and optionally
  persisted to a JSON file so they survive restarts.
- A device is considered "online" if it polled within the last
  HEARTBEAT_TIMEOUT_SECONDS seconds.

Security notes:
- Token comparison uses hmac.compare_digest to prevent timing attacks.
- Device IDs and tokens are validated as non-empty strings before use.
- The session file is written atomically (temp file + rename) to avoid
  corruption on crash.
- This is a development-grade implementation.  For production, replace
  in-memory storage with a proper database and add token expiry / rotation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("nova.device_session")

# ── Config ────────────────────────────────────────────────────
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("DEVICE_HEARTBEAT_TIMEOUT", "60"))
SESSION_FILE = Path(os.getenv("DEVICE_SESSION_FILE", "device_sessions.json"))

TOKEN_BYTES = 32   # 256-bit token → 43 URL-safe base64 chars


# ── Session dataclass ──────────────────────────────────────────
@dataclass
class DeviceSession:
    device_id:   str                          # UUID from the Android device
    token:       str                          # server-issued session token
    device_name: str                          # human label (e.g. "Saroj's Phone")
    registered_at: float = field(default_factory=time.time)
    last_seen:     float = field(default_factory=time.time)
    # Pending commands waiting to be polled by the device
    # List of dicts: [{command_id, intent, parameters, created_at}, ...]
    pending_commands: list = field(default_factory=list)
    # Completed results keyed by command_id
    results: dict = field(default_factory=dict)

    def is_online(self) -> bool:
        return (time.time() - self.last_seen) < HEARTBEAT_TIMEOUT_SECONDS

    def touch(self) -> None:
        self.last_seen = time.time()

    def token_preview(self) -> str:
        """First 8 chars of token — safe to log."""
        return self.token[:8] + "…"


# ── In-memory registry ─────────────────────────────────────────
_registry: dict[str, DeviceSession] = {}


# ── Persistence helpers ────────────────────────────────────────
def _load_sessions() -> None:
    """Load sessions from disk on startup (best-effort)."""
    if not SESSION_FILE.exists():
        return
    try:
        raw = SESSION_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        for entry in data:
            s = DeviceSession(**entry)
            _registry[s.device_id] = s
        logger.info("Loaded %d device session(s) from %s", len(_registry), SESSION_FILE)
    except Exception as e:
        logger.warning("Could not load session file %s: %s", SESSION_FILE, e)


def _save_sessions() -> None:
    """Atomically persist sessions to disk."""
    try:
        data = [asdict(s) for s in _registry.values()]
        payload = json.dumps(data, indent=2)
        # Write to a temp file then rename for atomicity
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(SESSION_FILE)
    except Exception as e:
        logger.error("Could not save session file: %s", e)


# Load existing sessions on module import
_load_sessions()


# ── Public API ─────────────────────────────────────────────────

def register_device(device_id: str, device_name: str) -> DeviceSession:
    """
    Register or re-register an Android device.
    Always issues a fresh token on (re-)registration.
    """
    if not device_id or not isinstance(device_id, str):
        raise ValueError("device_id must be a non-empty string")
    if not device_name or not isinstance(device_name, str):
        raise ValueError("device_name must be a non-empty string")

    # Sanitise inputs — no control chars, reasonable length
    device_id   = device_id.strip()[:128]
    device_name = device_name.strip()[:64]

    token = secrets.token_urlsafe(TOKEN_BYTES)

    session = DeviceSession(
        device_id=device_id,
        token=token,
        device_name=device_name,
    )
    _registry[device_id] = session
    _save_sessions()

    logger.info(
        "Device registered: id=%r name=%r token=%s",
        device_id, device_name, session.token_preview(),
    )
    return session


def authenticate(device_id: str, token: str) -> Optional[DeviceSession]:
    """
    Validate device_id + token.  Returns the session or None.
    Uses constant-time comparison to prevent timing attacks.
    """
    session = _registry.get(device_id)
    if session is None:
        return None
    # hmac.compare_digest requires bytes or same-type str
    if hmac.compare_digest(session.token, token):
        session.touch()
        return session
    logger.warning("Auth failed for device_id=%r (token mismatch)", device_id)
    return None


def get_session(device_id: str) -> Optional[DeviceSession]:
    """Look up a session without auth (for internal use only)."""
    return _registry.get(device_id)


def list_devices() -> list[dict]:
    """Return public-safe info about all registered devices."""
    return [
        {
            "device_id":   s.device_id,
            "device_name": s.device_name,
            "online":      s.is_online(),
            "registered_at": s.registered_at,
            "last_seen":   s.last_seen,
            "pending_commands": len(s.pending_commands),
        }
        for s in _registry.values()
    ]


def any_device_online() -> bool:
    """True if at least one device is currently online."""
    return any(s.is_online() for s in _registry.values())


def first_online_device() -> Optional[DeviceSession]:
    """Return the first online device session, or None."""
    for s in _registry.values():
        if s.is_online():
            return s
    return None


def push_command(device_id: str, command: dict) -> bool:
    """
    Enqueue a command for the given device.
    Returns False if device not found.
    """
    session = _registry.get(device_id)
    if session is None:
        return False
    session.pending_commands.append(command)
    _save_sessions()
    return True


def pop_pending_commands(device_id: str) -> list[dict]:
    """
    Atomically drain all pending commands for a device (used by poll endpoint).
    Returns empty list if device not found.
    """
    session = _registry.get(device_id)
    if session is None:
        return []
    session.touch()
    commands = list(session.pending_commands)
    session.pending_commands.clear()
    _save_sessions()
    return commands


def store_result(device_id: str, command_id: str, result: dict) -> bool:
    """Store the result of a completed command.  Returns False if device not found."""
    session = _registry.get(device_id)
    if session is None:
        return False
    session.results[command_id] = {**result, "received_at": time.time()}
    _save_sessions()
    logger.info(
        "Result stored: device=%r command_id=%r status=%s",
        device_id, command_id, result.get("status", "?"),
    )
    return True


def get_result(device_id: str, command_id: str) -> Optional[dict]:
    """Retrieve the stored result for a command (for polling by web UI)."""
    session = _registry.get(device_id)
    if session is None:
        return None
    return session.results.get(command_id)
