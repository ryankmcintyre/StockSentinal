"""Integration tests for auth routes (/auth/login, /auth/callback, /auth/logout)."""

from datetime import datetime
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import PKCE_COOKIE_NAME, SESSION_COOKIE_NAME, encode_pkce_cookie, encode_session_cookie
from app.database import get_uow
from app.main import app
from app.models import Base, User
from app.unit_of_work import SqlAlchemyUnitOfWork


@pytest.fixture(autouse=True)
def _setup_db():
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
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app, follow_redirects=False)


def test_login_page_renders(client):
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "Sign In" in resp.text


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


def test_callback_missing_pkce_cookie(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    resp = client.get("/auth/callback?code=somecode")
    assert resp.status_code == 303
    assert "error=invalid_state" in resp.headers["location"]


def test_callback_success_sets_session_cookie(client, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret-key")
    monkeypatch.setenv("SESSION_SECRET_KEY", "test-session-secret")

    from jose import jwt

    token = jwt.encode(
        {
            "sub": "new-user",
            "email": "new@example.com",
            "user_metadata": {"full_name": "New User"},
        },
        "test-secret-key",
        algorithm="HS256",
    )

    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"access_token": token}

    monkeypatch.setattr("app.main.httpx.post", lambda *args, **kwargs: response)

    pkce_cookie = encode_pkce_cookie("verifier")
    resp = client.get(
        "/auth/callback?code=somecode",
        cookies={PKCE_COOKIE_NAME: pkce_cookie},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert resp.cookies.get(SESSION_COOKIE_NAME)


def test_protected_route_redirects_to_login(client):
    resp = client.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
