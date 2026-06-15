"""CSRF protection helpers and middleware."""

import logging
import secrets
from hmac import compare_digest

from fastapi import Form, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from jinja2 import pass_context
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_session_secret_key

CSRF_COOKIE_NAME = "ss_csrf"
CSRF_FIELD_NAME = "csrf_token"
CSRF_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
logger = logging.getLogger(__name__)


def _is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _get_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_session_secret_key(), salt="ss-csrf")


def create_csrf_token() -> str:
    """Create a signed CSRF token."""
    return _get_serializer().dumps({"token": secrets.token_urlsafe(32)})


def _is_valid_signed_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        data = _get_serializer().loads(token, max_age=CSRF_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return False
    except RuntimeError:
        logger.debug(
            "CSRF token validation failed: session secret key not configured",
            exc_info=True,
        )
        return False
    return isinstance(data, dict) and isinstance(data.get("token"), str)


async def validate_csrf(
    request: Request,
    csrf_token: str | None = Form(None),
) -> None:
    """Validate a signed double-submit CSRF token from a form POST or fetch header."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME)
    submitted_token = csrf_token if csrf_token is not None else request.headers.get("x-csrf-token")
    if not _is_valid_signed_token(cookie_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    if not isinstance(submitted_token, str) or not compare_digest(cookie_token, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


class CSRFMiddleware(BaseHTTPMiddleware):
    """Make CSRF tokens available to safe requests that render forms."""

    async def dispatch(self, request: Request, call_next):
        if request.method not in _SAFE_METHODS:
            request.state.csrf_token = request.cookies.get(CSRF_COOKIE_NAME)
            return await call_next(request)

        token = request.cookies.get(CSRF_COOKIE_NAME)
        should_set_cookie = not _is_valid_signed_token(token)
        if should_set_cookie:
            try:
                token = create_csrf_token()
            except RuntimeError:
                token = ""
        request.state.csrf_token = token

        response = await call_next(request)
        if should_set_cookie and token:
            response.set_cookie(
                key=CSRF_COOKIE_NAME,
                value=token,
                max_age=CSRF_MAX_AGE_SECONDS,
                httponly=True,
                samesite="lax",
                secure=_is_https(request),
            )
        return response


@pass_context
def csrf_token_for_template(context) -> str:
    """Return the request CSRF token for Jinja templates."""
    request = context.get("request")
    if request is None:
        return ""
    return getattr(request.state, "csrf_token", "")
