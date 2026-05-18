"""Tests for the splash page (GET /) and root route auth-branching."""

from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_authenticated_uow, get_uow
from app.main import app
from app.models import Base, User
from app.unit_of_work import SqlAlchemyUnitOfWork


@pytest.fixture()
def engine_and_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    db = TestingSession()
    db.add(
        User(
            id="test-user-id",
            email="user@example.com",
            display_name="Test User",
            created_at=datetime.now(),
        )
    )
    db.commit()
    db.close()

    yield engine, TestingSession

    engine.dispose()


@pytest.fixture()
def client(engine_and_session):
    _, TestingSession = engine_and_session

    def override_get_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session)
        finally:
            session.close()

    def override_get_authenticated_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session, user_id="test-user-id")
        finally:
            session.close()

    app.dependency_overrides[get_uow] = override_get_uow
    app.dependency_overrides[get_authenticated_uow] = override_get_authenticated_uow
    c = TestClient(app, follow_redirects=False)
    yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Anonymous visitor tests
# ---------------------------------------------------------------------------

def test_anonymous_get_root_returns_splash(client):
    """Unauthenticated GET / must return the splash page (not redirect to login)."""
    resp = client.get("/")
    assert resp.status_code == 200


def test_splash_contains_app_description(client):
    """Splash page should describe what the app does."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Sell" in resp.text
    assert "Trim" in resp.text
    assert "Hold" in resp.text


def test_splash_contains_sign_in_link(client):
    """Splash page must include a link to the sign-in page."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "/auth/login" in resp.text


def test_splash_does_not_show_authenticated_nav_links(client):
    """Anonymous splash page must not expose Rules or Add Position nav links."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/rules"' not in resp.text
    assert 'href="/add"' not in resp.text


def test_splash_does_not_redirect_to_login(client):
    """Anonymous GET / must NOT redirect to /auth/login — it renders the splash."""
    resp = client.get("/")
    # 3xx redirect would mean the user is bounced to login without seeing the splash
    assert resp.status_code < 300


# ---------------------------------------------------------------------------
# Authenticated visitor tests
# ---------------------------------------------------------------------------

def test_authenticated_get_root_returns_portfolio(client, engine_and_session):
    """Authenticated GET / must render the portfolio dashboard, not the splash."""
    _, TestingSession = engine_and_session

    def fake_get_user_id(_request):
        return "test-user-id"

    def fake_session_local():
        return TestingSession()

    with (
        patch("app.main.get_current_user_id", side_effect=fake_get_user_id),
        patch("app.main.SessionLocal", side_effect=fake_session_local),
    ):
        resp = client.get("/")

    assert resp.status_code == 200
    # Portfolio page has the positions table / summary section, splash does not
    assert "portfolio" in resp.text.lower() or "positions" in resp.text.lower()
    # Splash-specific headline should not appear
    assert "splash-headline" not in resp.text
