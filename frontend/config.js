// Nova AI Assistant — Frontend Configuration
//
// ── Backend API ───────────────────────────────────────────────────────────────
// Change API_BASE_URL to match your backend:
//   Local dev  : "http://127.0.0.1:8000"
//   Render/prod: "https://nava-ai-assistant.onrender.com"
//
// Do NOT hardcode this URL inside script.js.

window.API_BASE_URL = "http://nava-ai-assistant-1.onrender.com";

// ── Supabase (frontend — anon key only, safe to expose) ──────────────────────
// Used by auth.js for Email + Password authentication with Supabase Auth.
// Get these from your Supabase project: Settings → API
//
//   SUPABASE_URL      : Project URL  (e.g. https://xxxx.supabase.co)
//   SUPABASE_ANON_KEY : Anon/public key (safe to use in the browser with RLS)
//
// NEVER put the service_role key or JWT secret here.

window.SUPABASE_URL      = "https://wimbyefehhbopddfygbn.supabase.co";
window.SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6IndpbWJ5ZWZlaGhib3BkZGZ5Z2JuIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODQwODM5NjEsImV4cCI6MjA5OTY1OTk2MX0.jRJSCQVndF6DSs79SSOK1f1Gqo4fzVRPILCYLcP6MU8";
