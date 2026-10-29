"""
Nova AI Assistant — mock_device.py

Standalone script that simulates an Android companion device.
Polls the backend exactly as the real Android app would, and returns
realistic (simulated) results for each command type.

Use this to test the web → backend → device → result pipeline without
needing a real Android phone.

Usage:
    python mock_device.py                          # uses .env settings
    python mock_device.py --url http://127.0.0.1:8000

Environment variables (or pass via CLI):
    MOCK_DEVICE_BASE_URL   — backend base URL       (default: http://127.0.0.1:8000)
    MOCK_DEVICE_ID         — device UUID             (default: mock-device-001)
    MOCK_DEVICE_NAME       — display name            (default: Mock Phone)
    NOVA_ADMIN_KEY         — admin key for register  (required)
    MOCK_POLL_INTERVAL     — seconds between polls   (default: 3)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL     = os.getenv("MOCK_DEVICE_BASE_URL", "http://127.0.0.1:8000")
DEFAULT_DEVICE_ID    = os.getenv("MOCK_DEVICE_ID",    "mock-device-001")
DEFAULT_DEVICE_NAME  = os.getenv("MOCK_DEVICE_NAME",  "Mock Phone (Dev)")
POLL_INTERVAL        = float(os.getenv("MOCK_POLL_INTERVAL", "3"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MOCK] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("mock_device")


# ---------------------------------------------------------------------------
# Simulated command handlers
# Each returns a result dict matching the server contract:
#   {status, detail, data}
# ---------------------------------------------------------------------------

def handle_call_contact(params: dict) -> dict:
    """Simulate searching contacts and opening the dialer."""
    contact_name  = params.get("contact_name", "")
    phone_number  = params.get("phone_number", "")
    use_auto_call = params.get("use_action_call", False)

    if phone_number:
        action = "ACTION_CALL" if use_auto_call else "ACTION_DIAL"
        logger.info("  → [MOCK] %s: tel:%s", action, phone_number)
        return {
            "status": "success",
            "detail": f"Dialer opened with number {phone_number} via {action}.",
            "data":   {"number_dialled": phone_number, "action": action},
        }

    if contact_name:
        # Simulate contact search — return 2 mock matches
        logger.info("  → [MOCK] Searching contacts for: %r", contact_name)
        time.sleep(0.3)
        mock_contacts = [
            {"name": f"{contact_name} (Work)",  "phone_numbers": ["+919876543210"]},
            {"name": f"{contact_name} (Mobile)", "phone_numbers": ["+919123456789"]},
        ]
        logger.info("  → [MOCK] Found %d matches", len(mock_contacts))
        return {
            "status": "success",
            "detail": f"Found {len(mock_contacts)} contacts matching '{contact_name}'. "
                      "Multiple matches — web UI should ask user to choose.",
            "data": {"matches": mock_contacts, "query": contact_name},
        }

    return {"status": "failed", "detail": "Neither contact_name nor phone_number provided.", "data": {}}


def handle_send_sms(params: dict) -> dict:
    phone  = params.get("phone_number", "")
    body   = params.get("message_body", "")
    logger.info("  → [MOCK] SMS Intent: to=%s body=%r", phone, body[:40])
    time.sleep(0.2)
    return {
        "status": "success",
        "detail": f"SMS app opened pre-filled to {phone}. User must press Send.",
        "data":   {"to": phone, "preview": body[:80]},
    }


def handle_set_alarm(params: dict) -> dict:
    hour   = params.get("hour", 0)
    minute = params.get("minute", 0)
    label  = params.get("label", "Nova Alarm")
    logger.info("  → [MOCK] AlarmClock Intent: %02d:%02d  label=%r", hour, minute, label)
    time.sleep(0.1)
    return {
        "status": "success",
        "detail": f"Alarm set for {hour:02d}:{minute:02d} — '{label}'.",
        "data":   {"hour": hour, "minute": minute, "label": label},
    }


def handle_get_contacts(params: dict) -> dict:
    query = params.get("query", "")
    logger.info("  → [MOCK] READ_CONTACTS query=%r", query)
    time.sleep(0.2)
    mock_results = [
        {"name": f"{query} Singh",  "phone_numbers": ["+911234567890"]},
        {"name": f"{query} Sharma", "phone_numbers": ["+919988776655", "+911122334455"]},
    ]
    return {
        "status": "success",
        "detail": f"Found {len(mock_results)} contacts.",
        "data":   {"contacts": mock_results},
    }


HANDLERS = {
    "CALL_CONTACT": handle_call_contact,
    "SEND_SMS":     handle_send_sms,
    "SET_ALARM":    handle_set_alarm,
    "GET_CONTACTS": handle_get_contacts,
}


# ---------------------------------------------------------------------------
# Mock device class
# ---------------------------------------------------------------------------
class MockDevice:
    def __init__(self, base_url: str, device_id: str, device_name: str):
        self.base_url    = base_url.rstrip("/")
        self.device_id   = device_id
        self.device_name = device_name
        self.token: Optional[str] = None
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _device_headers(self) -> dict:
        return {
            "X-Device-Id":    self.device_id,
            "X-Device-Token": self.token or "",
        }

    def register(self, admin_key: str) -> bool:
        """Register with the server and obtain a session token."""
        url = f"{self.base_url}/device/register"
        try:
            resp = self.session.post(url, json={
                "device_id":   self.device_id,
                "device_name": self.device_name,
            }, headers={"X-Admin-Key": admin_key}, timeout=10)

            if resp.status_code == 200:
                data = resp.json()
                self.token = data["token"]
                logger.info(
                    "Registered as '%s' (token=%s...)",
                    self.device_name, self.token[:8]
                )
                return True
            else:
                logger.error("Registration failed HTTP %d: %s", resp.status_code, resp.text[:200])
                return False
        except Exception as e:
            logger.error("Registration error: %s", e)
            return False

    def poll(self) -> list[dict]:
        """Poll for pending commands."""
        url = f"{self.base_url}/device/poll"
        try:
            resp = self.session.get(url, headers=self._device_headers(), timeout=10)
            if resp.status_code == 200:
                return resp.json().get("commands", [])
            elif resp.status_code == 401:
                logger.warning("Poll: 401 — token expired? Re-registering next cycle.")
                self.token = None
            else:
                logger.warning("Poll: HTTP %d", resp.status_code)
        except requests.exceptions.ConnectionError:
            logger.error("Poll: cannot connect to %s", self.base_url)
        except Exception as e:
            logger.error("Poll error: %s", e)
        return []

    def submit_result(self, command_id: str, status: str, detail: str, data: dict) -> bool:
        """Submit command result back to server."""
        url = f"{self.base_url}/device/result"
        try:
            resp = self.session.post(url,
                json={
                    "command_id": command_id,
                    "status":     status,
                    "detail":     detail,
                    "data":       data,
                },
                headers=self._device_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Result submitted: cmd=%s status=%s", command_id[:8], status)
                return True
            else:
                logger.warning("Result submit failed HTTP %d: %s", resp.status_code, resp.text[:100])
        except Exception as e:
            logger.error("Result submit error: %s", e)
        return False

    def handle_command(self, command: dict) -> None:
        """Dispatch a command to the appropriate handler and submit result."""
        cmd_id  = command.get("command_id", "?")
        intent  = command.get("intent", "?")
        params  = command.get("parameters", {})
        timeout = command.get("timeout_s", 30)

        logger.info("Command received: intent=%s cmd_id=%s", intent, cmd_id[:8])

        handler = HANDLERS.get(intent)
        if handler is None:
            logger.warning("Unknown intent: %r — returning failed", intent)
            self.submit_result(
                cmd_id, "failed",
                f"Mock device does not handle intent '{intent}'.", {}
            )
            return

        # Simulate a brief processing delay
        time.sleep(0.5)

        try:
            result = handler(params)
            self.submit_result(
                cmd_id,
                result["status"],
                result["detail"],
                result.get("data", {}),
            )
        except Exception as e:
            logger.exception("Handler error for %s: %s", intent, e)
            self.submit_result(cmd_id, "failed", f"Mock handler exception: {e}", {})

    def run(self, admin_key: str) -> None:
        """Main poll loop."""
        logger.info("Mock device starting: id=%r name=%r base=%s",
                    self.device_id, self.device_name, self.base_url)
        logger.info("Poll interval: %.1fs", POLL_INTERVAL)
        logger.info("Ctrl+C to stop.\n")

        # Register (retry until server is reachable)
        while True:
            if self.register(admin_key):
                break
            logger.info("Retrying registration in 5s…")
            time.sleep(5)

        logger.info("Polling started. Waiting for commands…\n")

        while True:
            if self.token is None:
                # Token was invalidated — re-register
                if not self.register(admin_key):
                    time.sleep(5)
                    continue

            commands = self.poll()
            for cmd in commands:
                self.handle_command(cmd)

            time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Nova Mock Android Device")
    parser.add_argument("--url",  default=DEFAULT_BASE_URL,    help="Backend base URL")
    parser.add_argument("--id",   default=DEFAULT_DEVICE_ID,   help="Device UUID")
    parser.add_argument("--name", default=DEFAULT_DEVICE_NAME, help="Device display name")
    args = parser.parse_args()

    admin_key = os.getenv("NOVA_ADMIN_KEY", "")
    if not admin_key:
        print("ERROR: NOVA_ADMIN_KEY is not set in .env or environment.")
        print("       Set it and try again.")
        sys.exit(1)

    device = MockDevice(
        base_url    = args.url,
        device_id   = args.id,
        device_name = args.name,
    )

    try:
        device.run(admin_key)
    except KeyboardInterrupt:
        logger.info("Mock device stopped.")


if __name__ == "__main__":
    main()
