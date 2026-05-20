"""Integration tests for email OTP auth routes."""

import base64
from datetime import datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth as auth_module
from app.auth import SESSION_COOKIE_NAME, encode_session_cookie
from app.database import get_uow
from app.main import app
from app.models import Base, User
from app.unit_of_work import SqlAlchemyUnitOfWork
from tests.csrf_utils import csrf_form_data


@pytest.fixture(autouse=True)
def _setup_db():
    auth_module._jwks_cache.clear()
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = TestingSession()
    db.add(User(id="existing-user", email="user@example.com", display_name="Test", created_at=datetime.now()))
    db.commit()
    db.close()

    def override_get_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session)
        finally:
            session.close()

    app.dependency_overrides[get_uow] = override_get_uow
    yield
    app.dependency_overrides.clear()
    auth_module._jwks_cache.clear()
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app, follow_redirects=False)


# ---------------------------------------------------------------------------
# POST /auth/email — request OTP code
# ---------------------------------------------------------------------------


def test_email_auth_request_sends_otp(client, monkeypatch):
    """Submitting an email sends OTP via Supabase and redirects with email_sent=1."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    post_calls = []

    def mock_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {}
        return response

    monkeypatch.setattr("app.main.httpx.post", mock_post)

    form_data = csrf_form_data(client, {"email": "newuser@example.com"})
    resp = client.post("/auth/email", data=form_data)

    assert resp.status_code == 303
    assert "email_sent=1" in resp.headers["location"]
    assert "email=newuser%40example.com" in resp.headers["location"]
    assert len(post_calls) == 1
    assert "/auth/v1/otp" in post_calls[0][0][0]
    payload = post_calls[0][1]["json"]
    assert payload["email"] == "newuser@example.com"
    assert payload["create_user"] is True
    # No PKCE material should be sent
    assert "code_challenge" not in payload
    assert "code_challenge_method" not in payload


def test_email_auth_request_not_configured(client, monkeypatch):
    """Redirects with error when Supabase is not configured."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    form_data = csrf_form_data(client, {"email": "test@example.com"})
    resp = client.post("/auth/email", data=form_data)

    assert resp.status_code == 303
    assert "error=not_configured" in resp.headers["location"]


def test_email_auth_request_supabase_failure(client, monkeypatch):
    """Redirects with error when Supabase OTP call fails."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    def mock_post(*args, **kwargs):
        raise Exception("network error")

    monkeypatch.setattr("app.main.httpx.post", mock_post)

    form_data = csrf_form_data(client, {"email": "test@example.com"})
    resp = client.post("/auth/email", data=form_data)

    assert resp.status_code == 303
    assert "error=email_send_failed" in resp.headers["location"]


# ---------------------------------------------------------------------------
# POST /auth/email/verify — verify OTP code
# ---------------------------------------------------------------------------


def _make_jwt_fixtures(monkeypatch, sub="new-email-user", email="newuser@example.com", user_metadata=None):
    """Helper to set up JWT signing fixtures for OTP verify tests."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwt

    def b64url_uint(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "RSA",
        "kid": "test-key",
        "use": "sig",
        "alg": "RS256",
        "n": b64url_uint(public_numbers.n),
        "e": b64url_uint(public_numbers.e),
    }
    token = jwt.encode(
        {
            "sub": sub,
            "email": email,
            "user_metadata": user_metadata or {},
        },
        private_pem,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )

    token_response = Mock()
    token_response.raise_for_status.return_value = None
    token_response.json.return_value = {"access_token": token}
    jwks_response = Mock()
    jwks_response.raise_for_status.return_value = None
    jwks_response.json.return_value = {"keys": [jwk]}

    monkeypatch.setattr("app.main.httpx.post", lambda *a, **kw: token_response)
    monkeypatch.setattr("app.auth.httpx.get", lambda *a, **kw: jwks_response)

    return token_response


def test_email_verify_success_new_user(client, monkeypatch):
    """Successful OTP verify creates a new user and sets session cookie."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    _make_jwt_fixtures(monkeypatch, sub="new-email-user", email="newuser@example.com")

    form_data = csrf_form_data(client, {"email": "newuser@example.com", "otp_code": "123456"})
    resp = client.post("/auth/email/verify", data=form_data)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert resp.cookies.get(SESSION_COOKIE_NAME)


def test_email_verify_success_existing_user(client, monkeypatch):
    """Successful OTP verify for existing user reuses the same local account."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    _make_jwt_fixtures(monkeypatch, sub="existing-user", email="user@example.com", user_metadata={"full_name": "Test"})

    form_data = csrf_form_data(client, {"email": "user@example.com", "otp_code": "654321"})
    resp = client.post("/auth/email/verify", data=form_data)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert resp.cookies.get(SESSION_COOKIE_NAME)


def test_email_verify_sends_correct_payload(client, monkeypatch):
    """Verify route sends the correct payload to Supabase /auth/v1/verify."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    post_calls = []

    import httpx as httpx_lib

    mock_response = Mock()
    mock_response.status_code = 400
    mock_response.text = "invalid token"
    mock_response.raise_for_status.side_effect = httpx_lib.HTTPStatusError(
        "bad request", request=Mock(), response=mock_response
    )

    def mock_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        return mock_response

    monkeypatch.setattr("app.main.httpx.post", mock_post)

    form_data = csrf_form_data(client, {"email": "test@example.com", "otp_code": "999999"})
    resp = client.post("/auth/email/verify", data=form_data)

    assert resp.status_code == 303
    assert len(post_calls) == 1
    assert "/auth/v1/verify" in post_calls[0][0][0]
    payload = post_calls[0][1]["json"]
    assert payload["email"] == "test@example.com"
    assert payload["token"] == "999999"
    assert payload["type"] == "email"


def test_email_verify_invalid_code(client, monkeypatch):
    """Redirects with error when Supabase rejects the OTP code."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    import httpx as httpx_lib

    mock_response = Mock()
    mock_response.status_code = 400
    mock_response.text = "Token has expired or is invalid"
    mock_response.raise_for_status.side_effect = httpx_lib.HTTPStatusError(
        "bad request", request=Mock(), response=mock_response
    )

    monkeypatch.setattr("app.main.httpx.post", lambda *a, **kw: mock_response)

    form_data = csrf_form_data(client, {"email": "test@example.com", "otp_code": "000000"})
    resp = client.post("/auth/email/verify", data=form_data)

    assert resp.status_code == 303
    assert "error=invalid_code" in resp.headers["location"]
    # No session cookie should be set
    assert not resp.cookies.get(SESSION_COOKIE_NAME)


def test_email_verify_expired_code(client, monkeypatch):
    """Redirects with error when OTP code is expired."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    import httpx as httpx_lib

    mock_response = Mock()
    mock_response.status_code = 403
    mock_response.text = "Token has expired"
    mock_response.raise_for_status.side_effect = httpx_lib.HTTPStatusError(
        "forbidden", request=Mock(), response=mock_response
    )

    monkeypatch.setattr("app.main.httpx.post", lambda *a, **kw: mock_response)

    form_data = csrf_form_data(client, {"email": "test@example.com", "otp_code": "111111"})
    resp = client.post("/auth/email/verify", data=form_data)

    assert resp.status_code == 303
    assert "error=invalid_code" in resp.headers["location"]
    assert not resp.cookies.get(SESSION_COOKIE_NAME)


def test_email_verify_not_configured(client, monkeypatch):
    """Redirects with error when Supabase is not configured."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    form_data = csrf_form_data(client, {"email": "test@example.com", "otp_code": "123456"})
    resp = client.post("/auth/email/verify", data=form_data)

    assert resp.status_code == 303
    assert "error=not_configured" in resp.headers["location"]


def test_email_verify_invalid_jwt(client, monkeypatch):
    """Redirects with error when JWT verification fails."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    token_response = Mock()
    token_response.raise_for_status.return_value = None
    token_response.json.return_value = {"access_token": "invalid.jwt.token"}

    jwks_response = Mock()
    jwks_response.raise_for_status.return_value = None
    jwks_response.json.return_value = {"keys": []}

    monkeypatch.setattr("app.main.httpx.post", lambda *a, **kw: token_response)
    monkeypatch.setattr("app.auth.httpx.get", lambda *a, **kw: jwks_response)

    form_data = csrf_form_data(client, {"email": "test@example.com", "otp_code": "123456"})
    resp = client.post("/auth/email/verify", data=form_data)

    assert resp.status_code == 303
    assert "error=invalid_token" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Login page shows email OTP form
# ---------------------------------------------------------------------------


def test_login_page_shows_email_form(client, monkeypatch):
    """Login page renders the email auth form when Supabase is configured."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "Send sign-in code" in resp.text
    assert 'name="email"' in resp.text


def test_login_page_shows_otp_form_after_email_sent(client, monkeypatch):
    """Login page shows OTP code entry form when email_sent=1."""
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    resp = client.get("/auth/login?email_sent=1&email=user@example.com")
    assert resp.status_code == 200
    assert "Check your email" in resp.text
    assert "6-digit code" in resp.text
    assert 'name="otp_code"' in resp.text
    assert "Verify code" in resp.text
    assert "/auth/email/verify" in resp.text
