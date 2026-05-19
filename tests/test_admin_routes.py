from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.database as database_module
from app.auth import SESSION_COOKIE_NAME, encode_session_cookie
from app.main import app
from app.models import Base, Position, User
from tests.csrf_utils import csrf_form_data


@pytest.fixture()
def session_maker(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    db = TestingSession()
    db.add_all(
        [
            User(
                id="admin-user",
                email="admin@example.com",
                display_name="Admin",
                created_at=datetime(2026, 5, 1),
                is_admin=True,
                tier="free",
            ),
            User(
                id="free-user",
                email="free@example.com",
                display_name="Free",
                created_at=datetime(2026, 5, 2),
                tier="free",
            ),
            Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=115.0,
                user_id="free-user",
            ),
        ]
    )
    db.commit()
    db.close()

    monkeypatch.setattr(database_module, "SessionLocal", TestingSession)
    yield TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client(session_maker):
    return TestClient(app)


def _login(client: TestClient, user_id: str) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, encode_session_cookie(user_id))


def test_admin_requires_authentication(client):
    resp = client.get("/admin", follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_non_admin_gets_403_even_with_valid_session_cookie(client):
    _login(client, "free-user")

    resp = client.get("/admin")

    assert resp.status_code == 403


def test_admin_sees_user_list_and_position_counts(client):
    _login(client, "admin-user")

    resp = client.get("/admin")

    assert resp.status_code == 200
    assert "admin@example.com" in resp.text
    assert "free@example.com" in resp.text
    assert "<td>1</td>" in resp.text


def test_admin_can_update_user_tier(client, session_maker, caplog):
    _login(client, "admin-user")

    resp = client.post(
        "/admin/users/free-user/tier",
        data=csrf_form_data(client, {"tier": "full_access"}),
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db = session_maker()
    try:
        assert db.query(User).filter(User.id == "free-user").one().tier == "full_access"
    finally:
        db.close()
    record = next(r for r in caplog.records if getattr(r, "action", None) == "update_tier")
    assert record.actor_id == "admin-user"
    assert record.target_id == "free-user"
    assert record.before == "free"
    assert record.after == "full_access"


def test_admin_can_update_admin_flag(client, session_maker):
    _login(client, "admin-user")

    resp = client.post(
        "/admin/users/free-user/admin",
        data=csrf_form_data(client, {"is_admin": "true"}),
        follow_redirects=False,
    )

    assert resp.status_code == 303
    db = session_maker()
    try:
        assert db.query(User).filter(User.id == "free-user").one().is_admin is True
    finally:
        db.close()


def test_cannot_demote_last_admin(client):
    _login(client, "admin-user")

    resp = client.post(
        "/admin/users/admin-user/admin",
        data=csrf_form_data(client),
        follow_redirects=False,
    )

    assert resp.status_code == 400
    assert "Cannot remove the last admin" in resp.text


def test_admin_post_requires_csrf(client):
    _login(client, "admin-user")

    resp = client.post(
        "/admin/users/free-user/tier",
        data={"tier": "full_access"},
        follow_redirects=False,
    )

    assert resp.status_code == 403
