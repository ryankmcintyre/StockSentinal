"""Authentication helpers: Supabase JWT verification, PKCE, and session cookies."""

import base64
import hashlib
import logging
import secrets
import time
from typing import Optional

import httpx
from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import (
    get_session_secret_key,
    get_supabase_jwks_url,
    get_supabase_jwt_secret,
)

logger = logging.getLogger(__name__)

# Session cookie name and max-age (1 week)
SESSION_COOKIE_NAME = "ss_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

# PKCE state cookie name and max-age (10 minutes)
PKCE_COOKIE_NAME = "ss_pkce"
PKCE_MAX_AGE_SECONDS = 10 * 60

_JWKS_CACHE_TTL_SECONDS = 600
_jwks_cache: dict[str, tuple[float, list[dict]]] = {}


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

    Modern Supabase projects usually sign access tokens with asymmetric signing
    keys discoverable via JWKS. Legacy projects may still use HS256 with a
    shared JWT secret. The ``sub`` claim contains the Supabase user UUID.
    """
    try:
        from jose import jwt

        header = jwt.get_unverified_header(token)
    except Exception:
        logger.debug("JWT header parsing failed", exc_info=True)
        return None

    algorithm = header.get("alg")
    if algorithm == "HS256":
        return _verify_supabase_hs256_jwt(token)
    return _verify_supabase_jwks_jwt(token, header)


def _verify_supabase_hs256_jwt(token: str) -> Optional[dict]:
    """Verify a legacy shared-secret Supabase access token."""
    jwt_secret = get_supabase_jwt_secret()
    if not jwt_secret:
        logger.warning("SUPABASE_JWT_SECRET is not set — cannot verify HS256 JWT")
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


def _verify_supabase_jwks_jwt(token: str, header: dict) -> Optional[dict]:
    """Verify a Supabase access token using the project's JWKS endpoint."""
    algorithm = header.get("alg")
    if not algorithm:
        logger.warning("JWT header is missing alg")
        return None

    jwk_key = _get_signing_key_for_header(header)
    if jwk_key is None:
        return None

    try:
        from jose import jwt

        return jwt.decode(
            token,
            jwk_key,
            algorithms=[algorithm],
            options={"verify_aud": False},
        )
    except Exception:
        logger.debug("JWKS JWT verification failed", exc_info=True)
        return None


def _get_signing_key_for_header(header: dict) -> Optional[dict]:
    """Return the matching JWK for a token header, if available."""
    keys = _load_supabase_jwks()
    if not keys:
        return None

    algorithm = header.get("alg")
    key_id = header.get("kid")
    if key_id:
        for key in keys:
            if key.get("kid") == key_id:
                return key
        logger.warning("No JWKS signing key found for kid=%s", key_id)
        return None

    matching_keys = [key for key in keys if key.get("alg") == algorithm]
    if len(matching_keys) == 1:
        return matching_keys[0]

    logger.warning("Unable to resolve JWKS signing key for alg=%s", algorithm)
    return None


def _load_supabase_jwks() -> Optional[list[dict]]:
    """Fetch and cache the Supabase JWKS document."""
    jwks_url = get_supabase_jwks_url()
    if not jwks_url:
        logger.warning("SUPABASE_URL is not set — cannot fetch JWKS")
        return None

    cached = _jwks_cache.get(jwks_url)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]

    try:
        response = httpx.get(jwks_url, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        logger.warning("Failed to fetch Supabase JWKS", exc_info=True)
        return None

    keys = payload.get("keys")
    if not isinstance(keys, list) or not all(isinstance(key, dict) for key in keys):
        logger.warning("Supabase JWKS response did not contain a valid keys list")
        return None

    _jwks_cache[jwks_url] = (now + _JWKS_CACHE_TTL_SECONDS, keys)
    return keys


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


class RequiresLoginException(Exception):
    """Raised by require_auth when the request is not authenticated."""



def is_https(request: Request) -> bool:
    """Return True if the request was made over HTTPS.

    Checks both the request scheme and the X-Forwarded-Proto header so this
    works correctly behind reverse proxies (e.g. Render, Heroku).
    """
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return request.url.scheme == "https"


def get_current_user_id(request: Request) -> Optional[str]:
    """Read the session cookie and return the user_id, or None if not authenticated."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    return decode_session_cookie(cookie)
