// ============================================================
//  Nova AI Assistant — auth.js
//  Session management for Supabase Auth (Email + Password).
//
//  Load order: config.js → auth.js → script.js
//
//  Session shape stored in localStorage under AUTH_STORAGE_KEY:
//    {
//      access_token:  string,   // Supabase JWT
//      refresh_token: string,   // used by refreshSession()
//      expires_at:    number,   // Unix timestamp (seconds)
//      user: {
//        id:           string,  // Supabase user UUID
//        email:        string,
//        display_name: string,  // set at signup, stored in user_metadata
//        full_name:    string,  // same value, kept for backwards compat
//        avatar_url:   string,
//      }
//    }
//
//  Public API:
//    getSession()                        → session | null
//    saveSession(session)                → void
//    clearSession()                      → void
//    isLoggedIn()                        → bool
//    getAuthHeader()                     → "Bearer <token>" | null
//    getUser()                           → user object | null
//    getDisplayName()                    → string | null
//    signUp(name, email, password)       → Promise<void>  — creates account
//    signIn(email, password)             → Promise<void>  — logs in, saves session
//    refreshSession()                    → Promise<bool>
//    logout()                            → void
//    requireAuth()                       → void
// ============================================================

'use strict';

const AUTH_STORAGE_KEY = 'nova_session';

// ── Supabase config helpers ───────────────────────────────────

function _supabaseUrl() {
  const u = (window.SUPABASE_URL || '').replace(/\/$/, '');
  if (!u) throw new Error('SUPABASE_URL is not set in config.js.');
  return u;
}

function _supabaseKey() {
  const k = window.SUPABASE_ANON_KEY || '';
  if (!k) throw new Error('SUPABASE_ANON_KEY is not set in config.js.');
  return k;
}

// ── Session read / write / clear ──────────────────────────────

function getSession() {
  try {
    const raw = localStorage.getItem(AUTH_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveSession(session) {
  if (!session || !session.access_token) {
    console.warn('[auth] saveSession: invalid session');
    return;
  }
  localStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(session));
}

function clearSession() {
  localStorage.removeItem(AUTH_STORAGE_KEY);
}

// ── Token state ───────────────────────────────────────────────

/** True if a valid, non-expired session exists (60 s buffer). */
function isLoggedIn() {
  const session = getSession();
  if (!session || !session.access_token) return false;
  if (!session.expires_at) return true;
  return session.expires_at > Math.floor(Date.now() / 1000) + 60;
}

/** Returns "Bearer <token>" or null. */
function getAuthHeader() {
  const session = getSession();
  return session?.access_token ? `Bearer ${session.access_token}` : null;
}

/** Returns the stored user object or null. */
function getUser() {
  const session = getSession();
  return session ? (session.user || null) : null;
}

/**
 * Best display name available.
 * Priority: localStorage cache → JWT display_name → JWT full_name.
 * Returns null only when no name is stored at all.
 */
function getDisplayName() {
  const cached = localStorage.getItem('nova_display_name');
  if (cached && cached.trim()) return cached.trim();

  const u = getUser();
  const name = u?.display_name || u?.full_name || '';
  if (name && !name.includes('@')) {
    localStorage.setItem('nova_display_name', name.trim());
    return name.trim();
  }
  return null;
}

// ── Build session from raw Supabase response ──────────────────

/**
 * Convert a raw Supabase token response into our session shape.
 * Decodes the JWT payload client-side for display only;
 * the backend re-verifies the token for every protected call.
 */
function _buildSession(data) {
  const access_token  = data.access_token  || '';
  const refresh_token = data.refresh_token || '';
  const expires_in    = parseInt(data.expires_in || '3600', 10);
  const expires_at    = Math.floor(Date.now() / 1000) + expires_in;

  let user = { id: '', email: '', display_name: '', full_name: '', avatar_url: '' };
  try {
    const b64Url  = access_token.split('.')[1] || '';
    const b64     = b64Url.replace(/-/g, '+').replace(/_/g, '/');
    const pad     = '=='.slice((b64.length % 4 === 0) ? 4 : b64.length % 4);
    const payload = JSON.parse(atob(b64 + pad));
    const meta    = payload.user_metadata || {};

    const displayName = meta.display_name || meta.full_name || meta.name || '';

    user = {
      id:           payload.sub   || '',
      email:        payload.email || meta.email || '',
      display_name: displayName,
      full_name:    displayName,   // kept for backwards compat with script.js checks
      avatar_url:   meta.avatar_url || '',
    };

    // Cache name locally so getDisplayName() is instant next call
    if (displayName && !displayName.includes('@')) {
      localStorage.setItem('nova_display_name', displayName.trim());
    }
  } catch (e) {
    console.warn('[auth] Could not decode JWT payload:', e);
  }

  return { access_token, refresh_token, expires_at, user };
}

// ── Error mapper ──────────────────────────────────────────────

/** Turn raw Supabase error JSON into a clean user-facing string. */
function _supabaseError(res, body) {
  const msg = (
    body?.error_description ||
    body?.msg               ||
    body?.message           ||
    body?.error             ||
    ''
  ).toLowerCase();

  if (res.status === 429)                              return 'Too many attempts. Please wait a minute and try again.';
  if (msg.includes('invalid login'))                   return 'Incorrect email or password.';
  if (msg.includes('email not confirmed'))             return 'Please confirm your email before signing in.';
  if (msg.includes('user already registered'))         return 'An account with this email already exists. Please log in.';
  if (msg.includes('password') && msg.includes('short')) return 'Password must be at least 6 characters.';
  if (msg.includes('unable to validate email'))        return 'Please enter a valid email address.';
  if (body?.error_description)                         return body.error_description;
  if (body?.message && body.message.length < 120)      return body.message;
  return `Request failed (${res.status}). Please try again.`;
}

// ── Sign Up ───────────────────────────────────────────────────

/**
 * Create a new account with email + password.
 * Passes `name` into user_metadata so it appears in every future JWT.
 *
 * On success: saves the session (Supabase returns a session immediately
 *   when email confirmation is disabled — the most common dev setup).
 *   If confirmation IS enabled, data.session will be null and we throw
 *   a friendly "check your email" message.
 *
 * @param {string} name
 * @param {string} email
 * @param {string} password
 * @returns {Promise<void>}
 * @throws {Error} with a human-readable message
 */
async function signUp(name, email, password) {
  const trimName  = (name  || '').trim();
  const trimEmail = (email || '').trim().toLowerCase();
  const trimPwd   = password || '';

  if (!trimName)              throw new Error('Please enter your name.');
  if (!trimEmail)             throw new Error('Please enter your email.');
  if (!trimPwd)               throw new Error('Please enter a password.');
  if (trimPwd.length < 6)    throw new Error('Password must be at least 6 characters.');

  const res = await fetch(`${_supabaseUrl()}/auth/v1/signup`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'apikey':       _supabaseKey(),
    },
    body: JSON.stringify({
      email:    trimEmail,
      password: trimPwd,
      data: {
        display_name: trimName,
        full_name:    trimName,
      },
    }),
    signal: AbortSignal.timeout(15000),
  });

  const body = await res.json().catch(() => ({}));

  if (!res.ok) {
    throw new Error(_supabaseError(res, body));
  }

  // If Supabase requires email confirmation, session will be null
  if (!body.access_token && !body.session?.access_token) {
    throw new Error('Account created! Please check your email to confirm your address, then log in.');
  }

  const sessionData = body.access_token ? body : body.session;
  saveSession(_buildSession(sessionData));
}

// ── Sign In ───────────────────────────────────────────────────

/**
 * Sign in with email + password.
 * On success: saves the session to localStorage.
 *
 * @param {string} email
 * @param {string} password
 * @returns {Promise<void>}
 * @throws {Error} with a human-readable message
 */
async function signIn(email, password) {
  const trimEmail = (email    || '').trim().toLowerCase();
  const trimPwd   = (password || '');

  if (!trimEmail) throw new Error('Please enter your email.');
  if (!trimPwd)   throw new Error('Please enter your password.');

  const res = await fetch(
    `${_supabaseUrl()}/auth/v1/token?grant_type=password`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'apikey':       _supabaseKey(),
      },
      body: JSON.stringify({ email: trimEmail, password: trimPwd }),
      signal: AbortSignal.timeout(15000),
    }
  );

  const body = await res.json().catch(() => ({}));

  if (!res.ok) {
    throw new Error(_supabaseError(res, body));
  }

  if (!body.access_token) {
    throw new Error('No session returned. Please try again.');
  }

  saveSession(_buildSession(body));
}

// ── Refresh session ───────────────────────────────────────────

/**
 * Silently refresh the access_token using the stored refresh_token.
 * Returns true on success, false on failure. Never throws.
 */
async function refreshSession() {
  const session = getSession();
  if (!session?.refresh_token) return false;

  try {
    const res = await fetch(
      `${_supabaseUrl()}/auth/v1/token?grant_type=refresh_token`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'apikey':       _supabaseKey(),
        },
        body: JSON.stringify({ refresh_token: session.refresh_token }),
        signal: AbortSignal.timeout(10000),
      }
    );

    if (!res.ok) return false;

    const data = await res.json();
    if (!data.access_token) return false;

    saveSession(_buildSession(data));
    return true;
  } catch {
    return false;
  }
}

// ── Logout ────────────────────────────────────────────────────

/**
 * Sign out: revoke token on Supabase, clear all local state, redirect.
 */
function logout() {
  const session = getSession();

  // Best-effort server-side revoke (keepalive = survives page unload)
  if (session?.access_token) {
    try {
      fetch(`${_supabaseUrl()}/auth/v1/logout`, {
        method:    'POST',
        headers: {
          'Content-Type':  'application/json',
          'apikey':        _supabaseKey(),
          'Authorization': `Bearer ${session.access_token}`,
        },
        keepalive: true,
      }).catch(() => {});
    } catch { /* ignore */ }
  }

  // Clear every Nova key from localStorage
  clearSession();
  localStorage.removeItem('nova_display_name');
  localStorage.removeItem('nova_name_skipped');

  window.location.href = 'login.html';
}

// ── Auth guard ────────────────────────────────────────────────

/** Redirect to login.html if not logged in. */
function requireAuth() {
  if (!isLoggedIn()) {
    clearSession();
    window.location.href = 'login.html';
  }
}
