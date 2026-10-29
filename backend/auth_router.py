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
  - Tokens are verified using SUPABASE_JWT_SECRET (HS256).
    The secret is never logged or returned to the client.
  - User IDs are UUIDs from Supabase auth — never generated here.
  - Passwords are handled by Supabase Auth — never stored or seen by the backend.
  - The frontend sends the access_token in the Authorization header;
    it never sends it as a query parameter.

Graceful degradation:
  - If SUPABASE_URL / SUPABASE_KEY are not set, all /auth/* routes
    return 503 with a clear message.
  - /chat uses optional auth: if auth is not configured, it proceeds
    without a user identity (backwards-compatible).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
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
# JWT verification
# ---------------------------------------------------------------------------

def _decode_supabase_jwt(token: str) -> dict:
    """
    Decode and verify a Supabase-issued JWT.

    Supabase projects use HS256 with the JWT secret from
    Project Settings → API → JWT Secret.

    Returns the decoded payload dict on success.
    Raises HTTPException 401 on any verification failure.
    """
    import jwt as pyjwt  # PyJWT — already in requirements.txt

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
            options={"verify_aud": False},  # Supabase tokens don't set aud by default
        )
        return payload
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except pyjwt.InvalidTokenError as e:
        logger.warning("JWT validation failed: %s", str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


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

    # If JWT secret is not configured, skip verification silently
    if not os.getenv("SUPABASE_JWT_SECRET", "").strip():
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
