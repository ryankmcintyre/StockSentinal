"""Authentication helpers: Supabase JWT verification, PKCE, and session cookies."""

import base64
import hashlib
import logging
import secrets
from typing import Optional

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_session_secret_key, get_supabase_jwt_secret

logger = logging.getLogger(__name__)

# Session cookie name and max-age (1 week)
SESSION_COOKIE_NAME = "ss_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

# PKCE state cookie name and max-age (10 minutes)
PKCE_COOKIE_NAME = "ss_pkce"
PKCE_MAX_AGE_SECONDS = 10 * 60


def _get_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_session_secret_key(), salt="ss-session")


def _get_pkce_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_session_secret_key(), salt="ss-pkce")


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for OAuth PKCE flow."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def encode_pkce_cookie(code_verifier: str) -> str:
    """Encode the PKCE code_verifier into a signed cookie value."""
    return _get_pkce_serializer().dumps({"verifier": code_verifier})


def decode_pkce_cookie(cookie_value: str) -> Optional[str]:
    """Decode and verify a PKCE cookie, returning the code_verifier or None."""
    try:
        data = _get_pkce_serializer().loads(cookie_value, max_age=PKCE_MAX_AGE_SECONDS)
        return data.get("verifier")
    except (BadSignature, SignatureExpired, Exception):
        return None


# ---------------------------------------------------------------------------
# Session cookie helpers
# ---------------------------------------------------------------------------


def encode_session_cookie(user_id: str) -> str:
    """Encode a user_id into a signed session cookie value."""
    return _get_serializer().dumps({"user_id": user_id})


def decode_session_cookie(cookie_value: str) -> Optional[str]:
    """Decode and verify a session cookie, returning the user_id or None."""
    try:
        data = _get_serializer().loads(cookie_value, max_age=SESSION_MAX_AGE_SECONDS)
        return data.get("user_id")
    except (BadSignature, SignatureExpired, Exception):
        return None


# ---------------------------------------------------------------------------
# JWT verification
# ---------------------------------------------------------------------------


def verify_supabase_jwt(token: str) -> Optional[dict]:
    """Verify a Supabase Auth JWT and return the claims dict, or None on failure.

    Supabase signs access tokens with HS256 using the project's JWT secret.
    The ``sub`` claim contains the Supabase user UUID.
    """
    jwt_secret = get_supabase_jwt_secret()
    if not jwt_secret:
        logger.warning("SUPABASE_JWT_SECRET is not set — cannot verify JWT")
        return None
    try:
        from jose import jwt

        claims = jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
        return claims
    except Exception:
        logger.debug("JWT verification failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


class RequiresLoginException(Exception):
    """Raised by require_auth when the request is not authenticated."""



def get_current_user_id(request: Request) -> Optional[str]:
    """Read the session cookie and return the user_id, or None if not authenticated."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    return decode_session_cookie(cookie)
