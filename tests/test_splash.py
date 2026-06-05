"""Tests for the splash page (GET /) and root route auth-branching."""

from datetime import datetime
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_authenticated_uow, get_optional_uow, get_uow
from app.main import app
from app.models import Base, User
from app.unit_of_work import SqlAlchemyUnitOfWork


class ProfileMenuParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._details_stack = []
        self.theme_label_seen = False
        self.logout_action_seen = False
        self._theme_label_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        class_names = attrs_dict.get("class", "").split()
        if tag == "details" and "profile-menu" in class_names:
            self._details_stack.append(True)
        elif tag == "details":
            self._details_stack.append(False)
        elif self.inside_profile_menu and tag == "span" and "theme-submenu-label" in class_names:
            self._theme_label_depth += 1
        elif self.inside_profile_menu and tag == "form" and attrs_dict.get("action") == "/auth/logout":
            self.logout_action_seen = True

    def handle_endtag(self, tag):
        if tag == "details" and self._details_stack:
            self._details_stack.pop()
        elif tag == "span" and self._theme_label_depth:
            self._theme_label_depth -= 1

    def handle_data(self, data):
        if self.inside_profile_menu and self._theme_label_depth and data.strip() == "Theme":
            self.theme_label_seen = True

    @property
    def inside_profile_menu(self):
        return any(self._details_stack)


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
    """Anonymous client — get_optional_uow yields None (not authenticated)."""
    _, TestingSession = engine_and_session

    def override_get_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session)
        finally:
            session.close()

    def override_get_optional_uow_anon():
        yield None

    app.dependency_overrides[get_uow] = override_get_uow
    app.dependency_overrides[get_optional_uow] = override_get_optional_uow_anon
    c = TestClient(app, follow_redirects=False)
    yield c
    app.dependency_overrides.clear()


@pytest.fixture()
def auth_client(engine_and_session):
    """Authenticated client — get_optional_uow yields a real scoped UoW."""
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

    def override_get_optional_uow_auth():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session, user_id="test-user-id")
        finally:
            session.close()

    app.dependency_overrides[get_uow] = override_get_uow
    app.dependency_overrides[get_authenticated_uow] = override_get_authenticated_uow
    app.dependency_overrides[get_optional_uow] = override_get_optional_uow_auth
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

def test_authenticated_get_root_returns_portfolio(auth_client):
    """Authenticated GET / must render the portfolio dashboard, not the splash."""
    resp = auth_client.get("/")

    assert resp.status_code == 200
    # Portfolio page has the positions table / summary section, splash does not
    assert "portfolio" in resp.text.lower() or "positions" in resp.text.lower()
    # Splash-specific headline should not appear
    assert "splash-headline" not in resp.text


def test_authenticated_get_root_shows_profile_theme_menu(auth_client):
    resp = auth_client.get("/")
    parser = ProfileMenuParser()
    parser.feed(resp.text)

    assert resp.status_code == 200
    assert 'aria-label="Profile"' in resp.text
    assert 'aria-label="Profile menu"' in resp.text
    assert 'class="theme-submenu-options"' in resp.text
    assert 'data-theme-option="system"' in resp.text
    assert 'data-theme-option="light"' in resp.text
    assert 'data-theme-option="dark"' in resp.text
    assert parser.theme_label_seen
    assert parser.logout_action_seen
    assert resp.text.count('action="/auth/logout"') == 1
    assert 'aria-pressed="false"' in resp.text
    assert 'role="menu"' not in resp.text
    assert 'role="menuitemradio"' not in resp.text
    assert "Test User" not in resp.text


# ---------------------------------------------------------------------------
# /privacy
# ---------------------------------------------------------------------------


def test_privacy_page_accessible_without_auth(client):
    resp = client.get("/privacy")
    assert resp.status_code == 200
    assert "Privacy Statement" in resp.text


def test_privacy_page_contains_key_sections(client):
    resp = client.get("/privacy")
    assert "What data is collected" in resp.text
    assert "How it is stored" in resp.text
    assert "Contact" in resp.text


def test_privacy_link_in_every_page_footer(client):
    for path in ["/privacy", "/auth/login"]:
        resp = client.get(path)
        assert resp.status_code == 200
        assert 'href="/privacy"' in resp.text, f"Privacy link missing on {path}"
        assert 'href="mailto:admin@stocksentinal.com"' in resp.text, f"Contact link missing on {path}"
