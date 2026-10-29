# Nova AI Assistant

**Nova** is a real-time AI personal assistant — not just a chatbot. It understands natural language (English and Hinglish), runs a multi-step agent loop to take real actions (open websites, search YouTube, check weather, send WhatsApp messages, make calls, draft emails, remember facts), supports voice input/output, image analysis, and PDF/TXT file uploads, and can control an Android phone through a companion app.

**Tech stack:** FastAPI · Groq (openai/gpt-oss-20b) · Supabase (Auth + DB) · HTML/CSS/JavaScript · Web Speech API · Lottie Animation · Android (Kotlin)

---

## Features

### Core AI
- **Controlled Agent Loop** — Nova runs up to 4 LLM ↔ tool round-trips per request with a 45-second hard timeout. It reasons across multiple steps, verifies each tool result, and returns a single grounded natural-language reply. Never claims success unless the tool confirmed it.
- **Primary + Fallback Models** — uses `openai/gpt-oss-20b` as primary (1 000 tps). On rate limit or timeout, automatically falls back to `openai/gpt-oss-120b` (500 tps, higher quality). Both are Groq free-tier models.
- **Conversation History** — when logged in, each conversation is persisted to Supabase. The agent loop injects the last 10 turn-pairs (20 messages) as context, so Nova remembers what was said earlier in the same conversation.
- **Personalized Welcome** — on page load Nova greets the user by name via `/chat/welcome`. The greeting is deterministic (no Groq call) when a name is known, and Groq-generated when it isn't.

### Tools Nova Can Use
| Tool | What it does | Confirmation | Runs on |
|---|---|---|---|
| `PLAY_YOUTUBE` | Opens YouTube search for a song or video | None | Browser |
| `SEARCH_YOUTUBE` | Opens YouTube search results for any query | None | Browser |
| `OPEN_WEBSITE` | Opens a URL in a new tab | ✅ Required | Browser |
| `GET_WEATHER` | Fetches live weather for any city via wttr.in | None | Backend |
| `WHATSAPP_MESSAGE` | Opens WhatsApp with a pre-filled message draft | ✅ Required | Browser |
| `PHONE_CALL` | Opens the browser's call prompt via `tel:` link | ✅ Required | Browser |
| `SEND_EMAIL` | Opens the default email client with a pre-filled draft | ✅ Required | Browser |
| `SAVE_MEMORY` | Stores a fact/preference for the logged-in user | None | Backend (Supabase) |
| `RECALL_MEMORY` | Retrieves stored facts/preferences | None | Backend (Supabase) |
| `DELETE_MEMORY` | Deletes one or all stored memories | ✅ Required | Backend (Supabase) |
| Android tools | Call, SMS, alarm, open app — via companion app | Varies | Android |

### Auth & Accounts
- **Email + Password Auth** — full signup/login/logout via Supabase Auth. JWTs are verified on the backend with `SUPABASE_JWT_SECRET` (never exposed to the frontend).
- **Silent Token Refresh** — the frontend refreshes an expired access token using the stored refresh token before redirecting to login.
- **Persistent User Memory** — facts saved with `SAVE_MEMORY` are scoped to the authenticated user and survive across sessions. Secrets (API keys, JWTs, base64 blobs) are blocked before storage.
- **Profile Upsert** — on first login, a profile row is created in Supabase automatically.

### Input / Output
- **Voice Input** — Web Speech API (hi-IN / Hinglish) with live interim transcript.
- **Voice Output** — Web Speech Synthesis API, preferring Hindi/English-Indian voices. Volume and rate tuned for natural delivery.
- **Image Analysis** — upload a JPG/PNG/WEBP (≤ 4 MB) via the `/vision` endpoint. Uses `qwen/qwen3.8-27b` (the Groq free-tier vision model as of Oct 2026). Image bytes are held in memory only — nothing is written to disk or stored.
- **Document Upload (PDF / TXT)** — upload a PDF or plain-text file (≤ 2 MB) and ask Nova questions about its content. Text is extracted in-memory via `pypdf` (PDF) or UTF-8 decoding (TXT), truncated to 12 000 characters, and injected as context into a standard Groq chat call. Nothing is stored permanently.
- **Confirmation Modal** — tools with medium/high risk (`OPEN_WEBSITE`, `WHATSAPP_MESSAGE`, `PHONE_CALL`, `SEND_EMAIL`, `DELETE_MEMORY`) always show a confirmation dialog before acting.
- **Thinking Indicator** — animated "Thinking → Working on it → Almost there" progress bubble while the agent loop runs. Transitions to "Completed ✓" before the reply renders.
- **Agent Steps Pill** — a compact read-only pill summarises which tool(s) the agent used (e.g. "☁ Checked weather", "▶ Searched YouTube"). Auto-removes after 2 seconds.
- **Markdown Rendering** — bot replies support `## headings`, `**bold**`, `*italic*`, `` `code` ``, bullet lists, and numbered lists. All content is HTML-escaped before rendering — no XSS possible.

### Settings
- **Per-User Preferences** — stored in `localStorage` under a user-ID-scoped key, so each account on the same browser has independent settings.
  - **Language** — Hindi / Hinglish (default) / English
  - **Response Style** — Short / Normal (default) / Detailed
  - **Voice Input** — enable/disable the mic button
  - **Auto-Speak** — read Nova's replies aloud automatically
- **Theme** — light/dark toggle, persisted in `localStorage`.

### Reliability & Security
- **Rate Limiting** — stdlib-only per-IP limiter: 20 RPM on `/chat`, 60 RPM on `/device/*`, 120 RPM globally.
- **Input Validation** — all tool parameters validated with Pydantic before any action is taken or any result is returned to the frontend.
- **Secret-free Logging** — API keys are only logged as masked previews (`gsk_Ab**...Xy12`). The JWT secret is never logged or returned. GROQ_API_KEY is deleted from the process namespace after the startup check.
- **Anon Key Safety** — `config.js` holds only the Supabase anon key (safe to expose with Row Level Security enabled). The service role key and JWT secret live only in `backend/.env`.

---

## How to Run Locally

### Prerequisites

- Python 3.11+
- A [Groq API key](https://console.groq.com) (free tier available)
- A [Supabase project](https://supabase.com) (free tier — needed for auth and memory)
- A modern browser (Chrome or Edge recommended for voice features)

### 1. Backend Setup

```bash
# Clone the repository
git clone <repo-url>
cd nava-ai-assistant/backend

# Create and activate a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

# Install dependencies (all versions pinned)
pip install -r requirements.txt

# Set up environment variables
copy .env.example .env       # Windows
# cp .env.example .env       # macOS / Linux

# Open .env and fill in GROQ_API_KEY, SUPABASE_URL, SUPABASE_KEY, SUPABASE_JWT_SECRET

# Start the backend server
uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

The backend will be available at `http://127.0.0.1:8000`.  
Verify: open `http://127.0.0.1:8000/` in a browser — you should see a JSON health response.

### 2. Frontend Setup

The frontend is plain HTML/CSS/JS — no build step needed.

**Option A — open directly:**
```
Open frontend/index.html in Chrome or Edge.
```

**Option B — serve with a local server (avoids browser `file://` restrictions):**
```bash
cd frontend
python -m http.server 5500
# Then open http://127.0.0.1:5500
```

Edit `frontend/config.js` to set your backend URL and Supabase credentials:

```js
window.API_BASE_URL   = "http://127.0.0.1:8000";   // or your Render URL
window.SUPABASE_URL   = "https://xxxx.supabase.co";
window.SUPABASE_ANON_KEY = "eyJ...";               // anon key only — safe to expose
```

### 3. Android Companion (optional)

See [`android/README.md`](android/README.md) for full setup.  
To test Android features without a real phone:

```bash
# With the backend already running, in a second terminal:
cd backend
NOVA_ADMIN_KEY=your_key python mock_device.py
```

---

## Environment Variables

All variables live in `backend/.env`. It is gitignored — **never commit it**.  
Use `backend/.env.example` as a template.

| Variable | Required | Description |
|---|---|---|
| `GROQ_API_KEY` | ✅ | Your Groq API key from [console.groq.com](https://console.groq.com) |
| `GROQ_MODEL` | Optional | Override primary model. Default: `openai/gpt-oss-20b` |
| `GROQ_VISION_MODEL` | Optional | Vision model. Default: `qwen/qwen3.8-27b` |
| `SUPABASE_URL` | ✅ (for auth/memory) | Supabase project URL |
| `SUPABASE_KEY` | ✅ (for auth/memory) | Supabase anon key (backend use with RLS) |
| `SUPABASE_JWT_SECRET` | ✅ (for auth) | JWT secret from Supabase → Settings → API. **Never expose to frontend.** |
| `SUPABASE_SERVICE_ROLE_KEY` | Optional | Required only for `DELETE /privacy/account` to remove the Supabase auth user row. Never expose to frontend. |
| `NOVA_ADMIN_KEY` | Optional* | Secret for Android device registration. Required for the companion app. |
| `AGENT_MAX_STEPS` | Optional | Max LLM ↔ tool round-trips per request. Default: `4` |
| `AGENT_TIMEOUT_S` | Optional | Hard wall-clock cap for the agent loop (seconds). Default: `45` |
| `AGENT_HISTORY_TURNS` | Optional | Conversation history pairs to inject. Default: `10` |
| `RATE_CHAT_RPM` | Optional | `/chat` rate limit per IP (requests/min). Default: `20` |
| `RATE_DEVICE_RPM` | Optional | `/device/*` rate limit per IP. Default: `60` |
| `RATE_GLOBAL_RPM` | Optional | All other routes rate limit per IP. Default: `120` |

---

## Architecture Overview

```
User (browser)
     │
     │  Text / Voice / Image / Document input
     ▼
Frontend  (HTML + JS + auth.js + prefs.js)
     │  POST /chat  { message, conversation_id?, user_name?, language?, response_style? }
     │  POST /vision  (multipart — image file + prompt)
     │  POST /upload  (multipart — PDF/TXT file + prompt)
     │  Authorization: Bearer <jwt>
     ▼
FastAPI Backend  (main.py)
     │
     ├─► Supabase Auth (verify JWT)
     │
     ├─► Conversation history (Supabase — last 10 turn-pairs)
     │
     ├─► Agent Loop (max 4 iterations, 45 s timeout)  [/chat]
     │       │
     │       ├─► Groq API  (openai/gpt-oss-20b → fallback openai/gpt-oss-120b)
     │       │       │
     │       │       ▼
     │       │   Plain text  ─────────────────────────► return to frontend
     │       │
     │       ├─► Tool call (validate with Pydantic)
     │       │       │
     │       │       ├── BROWSER tool (YouTube / website / WhatsApp / call / email)
     │       │       │       └─► synthesise URL, feed result to LLM
     │       │       │
     │       │       ├── SERVER tool (weather / memory)
     │       │       │       └─► execute on backend, feed result to LLM
     │       │       │
     │       │       └── ANDROID tool (call / SMS / alarm)
     │       │               └─► short-circuit → return android_tool_call
     │       │                   frontend dispatches to /device/command
     │       │
     │       └─► Final LLM call → natural-language reply
     │
     ├─► POST /vision   (image upload → in-memory base64 → Groq vision model → reply)
     │
     └─► POST /upload   (PDF/TXT → in-memory text extraction → Groq chat → reply)
```

---

## Limitations

### Browser (web frontend)

1. **Popup blocker** — `OPEN_WEBSITE` and `PLAY_YOUTUBE` use `window.open()`. If the browser's popup blocker fires, Nova shows a clickable fallback link rather than claiming success.

2. **Voice input: Chrome / Edge only** — `webkitSpeechRecognition` is not available in Firefox or Safari. Nova shows a clear unsupported-browser message rather than silently failing.

3. **TTS voice quality varies by OS** — Windows and Android typically have good hi-IN voices. macOS and Linux may fall back to a generic English voice with no error shown.

4. **Image analysis: JPEG / PNG / WEBP only, max 4 MB** — the vision endpoint rejects larger files and unsupported formats (e.g. GIF, HEIC, BMP). No video frame extraction.

5. **Document upload: PDF / TXT only, max 2 MB** — scanned / image-only PDFs yield no text (pypdf cannot OCR). Only the first 12 000 characters of extracted text are sent to the LLM; very long documents are silently truncated.

6. **WhatsApp / Phone / Email: drafts only** — Nova opens WhatsApp, the system dialer, or the default email client with pre-filled content, but *cannot* send automatically. The user must press Send / Call themselves. Nova says so honestly in every reply.

7. **Groq free-tier rate limits** — `openai/gpt-oss-20b` has ~1 000 tps on the free tier; the fallback `openai/gpt-oss-120b` has ~500 tps. Under sustained heavy load both may be exhausted and Nova will return a "try again" message.

8. **Agent loop: max 4 steps, 45 s** — complex multi-tool tasks are capped. If the agent times out mid-loop, the last partial result is returned with a warning.

9. **Memory requires login** — `SAVE_MEMORY` / `RECALL_MEMORY` / `DELETE_MEMORY` return an error for anonymous (unauthenticated) users. No guest memory.

10. **No real-time streaming** — responses are returned as a single JSON payload, not streamed. Long agent loops may feel slow on first use.

11. **Settings are browser-local** — preferences (language, voice, style) are stored in `localStorage` per device. They do not sync across browsers or devices.

### Android companion

12. **Companion app required** — phone actions (call, SMS, alarm, open app) only work when Nova Companion is installed, running in the foreground, and the relevant Android permissions are granted.

13. **Android action confirmation is one-way** — Nova can confirm the dialer or SMS compose screen was opened, but cannot confirm the user actually pressed "Call" or "Send".

14. **One device at a time** — the backend routes commands to the first registered online device. Multi-device is not implemented.

15. **Poll-based latency** — commands are received by the phone every 3–5 seconds (polling interval). WebSocket push would cut this but is not yet implemented.

---

## Interview / Demo Talking Points

**Why FastAPI?**  
FastAPI gives automatic request validation via Pydantic, async support, and auto-generated API docs at `/docs`. It's faster than Flask for I/O-bound workloads like proxying LLM calls and Supabase reads.

**Why Groq?**  
Groq's LPU hardware delivers very low latency — typically under 1 second for a 400-token response. This is critical for a voice assistant where lag directly hurts the experience.

**Why an agent loop instead of a single LLM call?**  
A single call can return a tool intent but cannot verify the result or chain tools. The loop lets Nova call GET_WEATHER, read the result, then write a grounded reply — rather than guessing. The hard caps (4 steps, 45 s) prevent runaway behaviour.

**Why tool-calling architecture?**  
Hard-coded responses like "playing music now" are dishonest and fragile. The LLM detects intent → backend validates strictly → frontend executes → result is reported honestly. This pattern scales to new tools without touching the LLM.

**Security decisions:**
- API keys and JWT secrets never leave the server; the frontend holds only the anon key (safe with RLS).
- All tool parameters are Pydantic-validated before any action is taken.
- `OPEN_WEBSITE` URL validation blocks `javascript:`, `file://`, and `chrome://` schemes.
- Android commands require a pre-shared admin key; device sessions use separate per-device tokens.
- Memory values are scanned for secret patterns (base64 blobs, `gsk_`, `sk-`, `eyJ` JWTs) and rejected before storage.
- Uploaded images and documents are processed in-memory and discarded immediately — nothing is written to disk or stored.
- Stdlib-only rate limiter with zero extra dependencies.

**Why a confirmation modal for OPEN_WEBSITE and communication tools?**  
Opening arbitrary URLs and drafting messages on behalf of the user are sensitive actions. The confirmation step prevents the LLM from acting on a misunderstood request, and the honest wording ("Nova cannot send — you must press Send") prevents the assistant from claiming false success.

**Android architecture:**  
All phone actions use Android Intents — the standard, permission-guarded mechanism. No root, no accessibility service abuse. The foreground service keeps the polling loop alive through battery optimisation.

---

## Project Structure

```
nava-ai-assistant/
├── backend/
│   ├── main.py              — FastAPI app, agent loop, Groq integration, rate limiter
│   ├── tool_registry.py     — Unified tool registry (browser + server tools)
│   ├── tools.py             — Legacy tool helpers (imported by registry)
│   ├── android_tools.py     — Android tool registry (CALL_CONTACT, SEND_SMS, etc.)
│   ├── android_router.py    — /device/* routes with admin key auth
│   ├── device_session.py    — In-memory device session registry
│   ├── auth_router.py       — /auth/* — Email + Password + JWT verification
│   ├── memory.py            — Conversation history (Supabase)
│   ├── user_memory.py       — Per-user fact/preference memory (Supabase)
│   ├── database.py          — Supabase client singleton
│   ├── mock_device.py       — Simulates an Android device for local testing
│   ├── schema.sql           — Base DB schema
│   ├── schema_v2.sql        — Adds conversations table
│   ├── schema_v3.sql        — Adds user_memories table
│   ├── requirements.txt     — All Python deps pinned to exact versions
│   └── .env.example         — Environment variable template (no real values)
├── frontend/
│   ├── index.html           — Main chat UI (auth guard, modals, input bar)
│   ├── login.html           — Login page
│   ├── signup.html          — Signup page
│   ├── script.js            — Chat logic, tool executors, voice I/O, agent UI,
│   │                          conversation history, settings panel
│   ├── auth.js              — Supabase Auth session management (signup/login/refresh/logout)
│   ├── prefs.js             — Per-user AI & UX preference store (localStorage)
│   ├── style.css            — All styling
│   ├── config.js            — API base URL + Supabase anon credentials
│   └── AI Robot.json        — Lottie animation asset
├── android/
│   ├── README.md            — Android companion full setup guide
│   └── nova_companion/      — Android Studio project (Kotlin)
├── docs/
│   └── agent-upgrade-plan.md
├── .gitignore
└── README.md
```

---

Developed by **Saroj Kumar**
