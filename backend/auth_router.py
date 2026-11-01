"""
Nova AI Assistant — auth_router.py

Supabase Auth integration: Email + Password + JWT verification.

Endpoints:
  GET  /auth/me           — returns the current user's profile;
                            upserts a profile row on first visit

JWT dependency:
  verify_token(token)     — FastAPI Depends() that validates a
                            Supabase-issued JWT and returns the
                            user payload.  Used by /chat and any
                            other route that needs authentication.

Security notes:
  - Tokens are verified using the Supabase project's public key,
    fetched once from <SUPABASE_URL>/auth/v1/.well-known/jwks.json
    and cached in memory.  The private key never leaves Supabase.
  - Supabase newer projects sign user JWTs with ES256 (ECDSA P-256).
    Older projects use HS256 with SUPABASE_JWT_SECRET.  Both are
    supported — the algorithm is read from the token header.
  - Secrets are never logged or returned to the client.
  - User IDs are UUIDs from Supabase auth — never generated here.
  - Passwords are handled by Supabase Auth — never stored or seen here.
  - The frontend sends the access_token in the Authorization header;
    it never sends it as a query parameter.

Graceful degradation:
  - If SUPABASE_URL / SUPABASE_KEY are not set, all /auth/* routes
    return 503 with a clear message.
  - /chat uses optional auth: if auth is not configured, it proceeds
    without a user identity (backwards-compatible).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Optional

import requests as _http_requests
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

logger = logging.getLogger("nova.auth")

router = APIRouter(prefix="/auth", tags=["Auth"])

# ── HTTPBearer extracts the token from Authorization: Bearer <token>
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Supabase availability guard
# ---------------------------------------------------------------------------

def _require_supabase() -> None:
    """Raise 503 if Supabase is not configured."""
    if not os.getenv("SUPABASE_URL", "").strip() or \
       not os.getenv("SUPABASE_KEY", "").strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Auth is not configured. "
                "Set SUPABASE_URL and SUPABASE_KEY in backend/.env "
                "to enable email + password login."
            ),
        )


# ---------------------------------------------------------------------------
# JWKS cache — fetched once, refreshed on kid-miss or after TTL
# ---------------------------------------------------------------------------

_jwks_lock         = threading.Lock()
_jwks_keys: dict   = {}      # {kid: constructed_public_key}  or {"__all__": [key, ...]}
_jwks_fetched_at   = 0.0
_JWKS_TTL_S        = 3600    # re-fetch after 1 hour

def _jwks_url() -> str:
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    return f"{url}/auth/v1/.well-known/jwks.json"

def _fetch_jwks(force: bool = False) -> None:
    """Fetch and parse JWKS into _jwks_keys.  Thread-safe."""
    global _jwks_keys, _jwks_fetched_at

    with _jwks_lock:
        # Double-checked locking
        if not force and _jwks_fetched_at and (time.monotonic() - _jwks_fetched_at < _JWKS_TTL_S):
            return

        url = _jwks_url()
        if not url.startswith("https://"):
            logger.warning("JWKS: SUPABASE_URL not set — skipping JWKS fetch")
            return

        try:
            resp = _http_requests.get(url, timeout=10,
                                      headers={"User-Agent": "Nova-AI-Assistant/5.0"})
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("JWKS fetch failed: %s", e)
            return

        new_keys: dict = {}
        import jwt as pyjwt
        for jwk in data.get("keys", []):
            alg = jwk.get("alg", "")
            kid = jwk.get("kid", "__default__")
            try:
                if alg.startswith("ES"):
                    from jwt.algorithms import ECAlgorithm
                    pub = ECAlgorithm.from_jwk(json.dumps(jwk))
                elif alg.startswith("RS"):
                    from jwt.algorithms import RSAAlgorithm
                    pub = RSAAlgorithm.from_jwk(json.dumps(jwk))
                else:
                    logger.warning("JWKS: unsupported alg %r — skipping", alg)
                    continue
                new_keys[kid] = (pub, alg)
                logger.info("JWKS: loaded key kid=%s alg=%s", kid, alg)
            except Exception as e:
                logger.error("JWKS: failed to load key kid=%s: %s", kid, e)

        _jwks_keys = new_keys
        _jwks_fetched_at = time.monotonic()
        logger.info("JWKS: cache updated, %d key(s) loaded", len(new_keys))


def _get_asymmetric_key(kid: Optional[str], alg: str):
    """
    Return the public key for the given kid.
    Triggers a fresh JWKS fetch if the kid is not found (handles key rotation).
    Returns None if no matching key can be found.
    """
    # Ensure cache is populated
    if not _jwks_keys:
        _fetch_jwks(force=True)

    # Try exact kid match first
    if kid and kid in _jwks_keys:
        return _jwks_keys[kid]

    # No kid in token or kid not found → try the only key if there's just one
    if len(_jwks_keys) == 1:
        return next(iter(_jwks_keys.values()))

    # Try re-fetching once (key rotation)
    _fetch_jwks(force=True)
    if kid and kid in _jwks_keys:
        return _jwks_keys[kid]
    if len(_jwks_keys) == 1:
        return next(iter(_jwks_keys.values()))

    return None


# ---------------------------------------------------------------------------
# JWT header peek — no verification, just reads alg + kid
# ---------------------------------------------------------------------------

def _peek_jwt_header(token: str) -> dict:
    """Decode the JWT header without any verification."""
    try:
        header_b64 = token.split(".")[0]
        padding = "=" * (4 - len(header_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(header_b64 + padding))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# JWT verification — supports ES256 (JWKS) and HS256 (shared secret)
# ---------------------------------------------------------------------------

def _decode_supabase_jwt(token: str) -> dict:
    """
    Decode and verify a Supabase-issued JWT.

    Algorithm detection (from token header):
      ES256 / RS256 → verify with Supabase JWKS public key
      HS256          → verify with SUPABASE_JWT_SECRET (older projects)

    Returns the decoded payload dict on success.
    Raises HTTPException 401 on any verification failure.
    """
    import jwt as pyjwt

    header = _peek_jwt_header(token)
    alg    = header.get("alg", "")
    kid    = header.get("kid")    # may be absent on HS256 tokens

    # ── Asymmetric algorithms (ES256, RS256) — use JWKS public key ──────────
    if alg in ("ES256", "ES384", "ES512", "RS256", "RS384", "RS512"):
        key_entry = _get_asymmetric_key(kid, alg)
        if key_entry is None:
            logger.error(
                "JWT: no public key found for kid=%r alg=%s "
                "(JWKS has %d key(s)). Check SUPABASE_URL in .env.",
                kid, alg, len(_jwks_keys),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Auth public key unavailable. Please try again shortly.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        pub_key, key_alg = key_entry
        try:
            payload = pyjwt.decode(
                token,
                pub_key,
                algorithms=[key_alg],
                options={"verify_aud": False},
            )
            return payload
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except pyjwt.InvalidTokenError as e:
            logger.warning("JWT validation failed (asymmetric): %s", e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # ── HS256 — use shared secret (older / HS256-configured projects) ───────
    if alg == "HS256":
        jwt_secret = os.getenv("SUPABASE_JWT_SECRET", "").strip()
        if not jwt_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "SUPABASE_JWT_SECRET is not set. "
                    "Add it to backend/.env (Project Settings → API → JWT Secret)."
                ),
            )
        try:
            payload = pyjwt.decode(
                token,
                jwt_secret,
                algorithms=["HS256"],
                options={"verify_aud": False},
            )
            return payload
        except pyjwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session expired. Please log in again.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except pyjwt.InvalidTokenError as e:
            logger.warning("JWT validation failed (HS256): %s", e)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # ── Unknown algorithm ────────────────────────────────────────────────────
    logger.warning("JWT: unsupported algorithm %r in token header", alg)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unsupported token algorithm.",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ---------------------------------------------------------------------------
# Pre-warm JWKS cache at import time (non-blocking, best-effort)
# ---------------------------------------------------------------------------

def _prewarm_jwks() -> None:
    """Fetch JWKS in a background thread so the first request isn't slow."""
    if os.getenv("SUPABASE_URL", "").strip():
        t = threading.Thread(target=_fetch_jwks, kwargs={"force": True}, daemon=True)
        t.start()

_prewarm_jwks()


# ---------------------------------------------------------------------------
# Public dependency: verify_token
# Use as: Depends(verify_token) on any route that requires auth.
# ---------------------------------------------------------------------------

def verify_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> dict:
    """
    FastAPI dependency: validates the Bearer token and returns the
    Supabase JWT payload.

    Raises HTTP 401 if:
      - No Authorization header is present
      - Token is expired
      - Token is invalid

    Example payload keys:
      sub       — Supabase user UUID (string)
      email     — user email
      role      — 'authenticated'
      exp       — expiry timestamp
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _decode_supabase_jwt(credentials.credentials)


# Optional variant — for routes that work with or without auth
def verify_token_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[dict]:
    """
    Like verify_token but returns None instead of raising 401
    when no token is present.  Used by /chat for graceful
    backwards-compatibility when auth is not configured.
    """
    if credentials is None or not credentials.credentials:
        return None

    # If Supabase URL is not configured at all, skip verification silently
    if not os.getenv("SUPABASE_URL", "").strip():
        return None

    try:
        return _decode_supabase_jwt(credentials.credentials)
    except HTTPException:
        return None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class UserProfile(BaseModel):
    id: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/me", response_model=UserProfile)
def get_current_user(user: dict = Depends(verify_token)):
    """
    Return the current user's profile, upserted into the profiles table.

    Called by the frontend immediately after the auth callback
    to confirm the session is valid and get profile data.

    The profile row is created/updated on first call after login.
    """
    _require_supabase()

    user_id    = user.get("sub", "")
    email      = user.get("email", "")

    # Supabase stores the display name in user_metadata (set at signup)
    user_meta  = user.get("user_metadata", {}) or {}
    full_name  = user_meta.get("full_name") or user_meta.get("display_name") or user_meta.get("name") or email
    avatar_url = user_meta.get("avatar_url") or ""

    # Upsert profile into Supabase profiles table (best-effort)
    _upsert_profile_safe(user_id, full_name, email, avatar_url)

    return UserProfile(
        id=user_id,
        email=email,
        full_name=full_name,
        avatar_url=avatar_url,
    )


def _upsert_profile_safe(
    user_id: str,
    full_name: str,
    email: str,
    avatar_url: str,
) -> None:
    """
    Upsert the user profile row.  Failures are logged but not raised
    — auth itself should not fail if the DB write fails.
    """
    try:
        from database import get_db
        db = get_db()
        if db is None:
            return

        db.table("profiles").upsert(
            {
                "id":         user_id,
                "full_name":  full_name,
                "email":      email,
                "avatar_url": avatar_url,
            },
            on_conflict="id",
        ).execute()
        logger.info("Profile upserted for user %s…", user_id[:8])
    except Exception as e:
        logger.warning("Profile upsert failed (non-fatal): %s", str(e))
