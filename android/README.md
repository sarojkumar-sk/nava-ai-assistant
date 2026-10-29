# Nova Companion — Android App

## Architecture Overview

```
User (voice/text)
      │
      ▼
Nova Web UI (browser)
      │  POST /chat
      ▼
FastAPI Backend
      │  type=android_tool_call
      ├─► Tool validation (android_tools.py)
      │       │  POST /device/command  (X-Admin-Key)
      │       ▼
      │  Device Session Registry
      │       │  GET /device/poll  (X-Device-Token)
      ▼       ▼
Android Companion App  ──────────────────────────────┐
  NovaService (foreground)                            │
      │                                               │
      ├─► CommandReceiver (receives poll response)    │
      │         │                                     │
      │         ▼                                     │
      │   ActionDispatcher                            │
      │    ├── CALL_CONTACT → ContactSearcher         │
      │    │      └──────────► Intent.ACTION_DIAL     │
      │    ├── SEND_SMS     → Intent (SMS app)        │
      │    ├── SET_ALARM    → AlarmClock.ACTION_SET_ALARM
      │    ├── PLAY_YOUTUBE → Intent (YouTube app)    │
      │    └── OPEN_APP     → Intent.ACTION_MAIN      │
      │                                               │
      └─► ResultReporter ──── POST /device/result ───┘
                                    │
                                    ▼
                            Backend stores result
                                    │
                            Web UI polls /device/result/{id}
                                    │
                            Nova responds honestly to user
```

## Security Model

| Who calls what | Auth required |
|---|---|
| Android app → `/device/register` | `X-Admin-Key` header |
| Android app → `/device/poll` | `X-Device-Id` + `X-Device-Token` headers |
| Android app → `/device/result` | `X-Device-Id` + `X-Device-Token` headers |
| Web UI → `/device/command` | `X-Admin-Key` header (never stored in browser localStorage) |
| Web UI → `/device/status` | Public (no auth) |

`NOVA_ADMIN_KEY` is a random secret set in `.env`. It is **never** sent to the browser — the web UI dispatches Android commands through the backend, not directly.

## Android Project Package Layout

```
android/nova_companion/
├── app/
│   ├── build.gradle.kts
│   └── src/main/
│       ├── AndroidManifest.xml
│       └── java/com/nova/companion/
│           ├── NovaService.kt          — Foreground service, polling loop
│           ├── CommandReceiver.kt      — Parses poll response, queues commands
│           ├── ActionDispatcher.kt     — Routes intents to correct Android API
│           ├── ContactSearcher.kt      — ContentResolver query, multiple-match handling
│           └── ResultReporter.kt       — POST /device/result with real status
├── build.gradle.kts
└── settings.gradle.kts
```

## Key Classes

### `NovaService`
- Runs as a **foreground service** (shows persistent notification)
- Polls `/device/poll` every 3 seconds using OkHttp
- Passes received commands to `CommandReceiver`
- Starts on device boot (via `RECEIVE_BOOT_COMPLETED` receiver)

### `CommandReceiver`
- Deserialises the JSON command envelope
- Validates `command_id`, `intent`, `parameters`, `timeout_s`
- Hands command to `ActionDispatcher`
- Ensures result is always submitted (success OR failure)

### `ActionDispatcher`
- `CALL_CONTACT` → calls `ContactSearcher`, then `Intent.ACTION_DIAL`
- `SEND_SMS`     → builds `Intent(Intent.ACTION_SENDTO, "smsto:...")` 
- `SET_ALARM`    → `AlarmClock.ACTION_SET_ALARM` Intent
- `PLAY_YOUTUBE` → deep-link Intent to YouTube app (falls back to browser)
- `OPEN_APP`     → `packageManager.getLaunchIntentForPackage()`
- All use `startActivity()` — no silent background execution

### `ContactSearcher`
- Queries `ContactsContract.CommonDataKinds.Phone` via `ContentResolver`
- Returns list of `{name, phoneNumbers[]}` matches
- Multiple matches → sends `GET_CONTACTS` result back so web UI can prompt user

### `ResultReporter`
- POST to `/device/result` with `X-Device-Id` + `X-Device-Token`
- Reports `success`, `cancelled`, `failed`, or `permission_denied`
- Uses `ActivityResultLauncher` to detect user cancellation
- Never reports success unless Android confirmed the action

## Required Permissions

See `AndroidManifest.xml` for the full list. Summary:

| Permission | Used for |
|---|---|
| `READ_CONTACTS` | Contact search (CALL_CONTACT, GET_CONTACTS) |
| `CALL_PHONE` | `ACTION_CALL` auto-dial (user must grant explicitly) |
| `FOREGROUND_SERVICE` | Keeping NovaService alive |
| `RECEIVE_BOOT_COMPLETED` | Auto-start after reboot |
| `INTERNET` | Polling backend, submitting results |
| `POST_NOTIFICATIONS` | Foreground service notification (Android 13+) |

**`CALL_PHONE` is a dangerous permission.** By default `ActionDispatcher` uses
`ACTION_DIAL` (opens dialer, user presses call). `ACTION_CALL` (auto-dials)
requires this permission AND user must explicitly grant it AND the
`use_action_call=true` parameter must be sent.

## Setup Guide

### 1. Prerequisites
- Android Studio Hedgehog (2023.1) or later
- Android SDK API 26+ (target API 34)
- Kotlin 1.9+
- A running Nova backend (`uvicorn main:app`)

### 2. Configure the app
Edit `app/src/main/res/values/config.xml`:
```xml
<string name="nova_base_url">https://your-server.com</string>
```
Or set it at runtime via the app's Settings screen.

### 3. Register the device
On first launch, the app calls:
```
POST /device/register
X-Admin-Key: <your NOVA_ADMIN_KEY>
Body: {"device_id": "<UUID>", "device_name": "My Phone"}
```
The server returns a `token`. The app stores this in EncryptedSharedPreferences.

### 4. Grant permissions
- Accept the notification permission on Android 13+
- Accept READ_CONTACTS when prompted
- Optionally grant CALL_PHONE for auto-dial

### 5. Start the service
`NovaService` starts automatically on registration and on boot.
You can also start/stop it from the app's main screen.

## Testing Without a Real Phone

Use `backend/mock_device.py`:
```bash
cd backend
# In one terminal — run the server
uvicorn main:app --reload

# In another terminal — run the mock device
NOVA_ADMIN_KEY=your_key python mock_device.py
```

The mock device polls the backend, simulates commands, and submits realistic
results. The web UI behaves identically to having a real phone connected.

## Known Limitations

1. **`ACTION_DIAL` vs `ACTION_CALL`**: Nova defaults to `ACTION_DIAL` which opens
   the dialer pre-filled but requires the user to press the green button.
   We cannot confirm the call was placed — only that the dialer was opened.

2. **SMS requires user confirmation**: The SMS Intent opens the Messages app
   pre-filled. Nova reports `success` when the Intent was dispatched, but
   cannot confirm the user actually pressed Send.

3. **YouTube deep-link**: If the YouTube app is not installed, Nova falls back
   to opening the search URL in the browser.

4. **Contact search is approximate**: Android's `ContentResolver` uses a
   LIKE query which may return more results than expected on large contact lists.

5. **No notification response tracking**: Nova can open a notification for an
   alarm, but cannot confirm the user interacted with it.

6. **One device at a time**: The current backend routes commands to the first
   online device. Multi-device support requires a device selector UI.

7. **Polling latency**: There is a 3-5 second delay between a command being
   dispatched and the device receiving it (poll-based, not push).
   WebSocket-based push would reduce this to <100ms but adds complexity.
