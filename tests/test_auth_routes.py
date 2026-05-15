"""Integration tests for auth routes (/auth/login, /auth/callback, /auth/logout)."""

import base64
from datetime import datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth as auth_module
from app.auth import PKCE_COOKIE_NAME, SESSION_COOKIE_NAME, encode_pkce_cookie, encode_session_cookie
from app.database import get_uow
from app.main import app
from app.models import Base, User
from app.unit_of_work import SqlAlchemyUnitOfWork


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


def test_login_page_renders(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "Sign In" in resp.text


def test_login_page_accepts_jwks_projects(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    resp = client.get("/auth/login")

    assert resp.status_code == 200
    assert "Supabase auth is not configured" not in resp.text


def test_login_redirects_if_already_logged_in(client):
    session_cookie = encode_session_cookie("existing-user")
    resp = client.get("/auth/login", cookies={SESSION_COOKIE_NAME: session_cookie})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_logout_clears_cookie(client):
    resp = client.post("/auth/logout")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
    assert SESSION_COOKIE_NAME not in resp.cookies or resp.cookies[SESSION_COOKIE_NAME] == ""


def test_callback_missing_code(client):
    resp = client.get("/auth/callback")
    assert resp.status_code == 303
    assert "error=missing_code" in resp.headers["location"]


def test_authorize_redirects_when_session_secret_missing(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    resp = client.get("/auth/google/authorize")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?error=not_configured"


def test_callback_redirects_when_session_secret_missing(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.delenv("SESSION_SECRET_KEY", raising=False)

    resp = client.get("/auth/callback?code=somecode")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?error=not_configured"


def test_authorize_redirects_when_publishable_key_missing(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)

    resp = client.get("/auth/google/authorize")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?error=not_configured"


def test_callback_redirects_when_publishable_key_missing(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)

    resp = client.get("/auth/callback?code=somecode")

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login?error=not_configured"


def test_callback_missing_pkce_cookie(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")
    resp = client.get("/auth/callback?code=somecode")
    assert resp.status_code == 303
    assert "error=invalid_state" in resp.headers["location"]


def test_callback_success_sets_session_cookie_with_jwks(client, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jose import jwt

    def b64url_uint(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

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
            "sub": "new-jwks-user",
            "email": "new@example.com",
            "user_metadata": {"full_name": "New User"},
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

    post_calls = []

    def mock_post(*args, **kwargs):
        post_calls.append((args, kwargs))
        return token_response

    monkeypatch.setattr("app.main.httpx.post", mock_post)
    monkeypatch.setattr("app.auth.httpx.get", lambda *args, **kwargs: jwks_response)

    pkce_cookie = encode_pkce_cookie("verifier")
    resp = client.get(
        "/auth/callback?code=somecode",
        cookies={PKCE_COOKIE_NAME: pkce_cookie},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert resp.cookies.get(SESSION_COOKIE_NAME)
    assert post_calls[0][1]["headers"]["apikey"] == "sb_publishable_test"


def test_protected_route_redirects_to_login(client):
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
