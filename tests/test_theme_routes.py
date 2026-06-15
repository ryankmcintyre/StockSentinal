from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.csrf import CSRF_COOKIE_NAME, create_csrf_token
from app.database import get_authenticated_uow, get_uow
from app.main import app
from app.models import Base, Position, StrategyRuleConfig, Theme, User
from app.unit_of_work import SqlAlchemyUnitOfWork
from tests.csrf_utils import csrf_form_data


@pytest.fixture(autouse=True)
def _setup_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    @event.listens_for(TestingSession.class_, "before_flush")
    def _assign_test_user_id(session, _flush_context, _instances):
        for obj in session.new:
            if isinstance(obj, User) and not obj.id:
                obj.id = "test-user-id"
            if isinstance(obj, Position) and obj.user_id is None:
                obj.user_id = "test-user-id"
            if isinstance(obj, (StrategyRuleConfig, Theme)) and obj.user_id is None:
                obj.user_id = "test-user-id"

    db = TestingSession()
    db.add(User(id="test-user-id", email="test@example.com", display_name="Test User", created_at=datetime.now(timezone.utc)))
    db.commit()
    db.close()

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

    with patch("app.main._refresh_single_position_task", return_value=None):
        app.dependency_overrides[get_uow] = override_get_uow
        app.dependency_overrides[get_authenticated_uow] = override_get_authenticated_uow
        yield TestingSession
        app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app)


def _json_csrf_headers(client):
    token = create_csrf_token()
    client.cookies.set(CSRF_COOKIE_NAME, token)
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "x-csrf-token": token,
    }


def _seed_theme(session_maker, name: str, user_id: str = "test-user-id") -> int:
    db = session_maker()
    try:
        db.merge(User(id=user_id, email=f"{user_id}@example.com"))
        db.flush()
        theme = Theme(name=name, user_id=user_id)
        db.add(theme)
        db.commit()
        return theme.id
    finally:
        db.close()


def _seed_position(session_maker, ticker: str = "AAPL", user_id: str = "test-user-id") -> int:
    db = session_maker()
    try:
        db.merge(User(id=user_id, email=f"{user_id}@example.com"))
        db.flush()
        position = Position(
            ticker=ticker,
            company_name=f"{ticker} Inc.",
            cost_basis=100.0,
            initial_purchase_date=date(2025, 1, 1),
            investment_type="long-term",
            current_price=125.0,
            user_id=user_id,
        )
        db.add(position)
        db.commit()
        return position.id
    finally:
        db.close()


def test_theme_json_create_requires_csrf(client):
    response = client.post(
        "/themes",
        json={"name": "AI"},
        headers={"accept": "application/json"},
    )

    assert response.status_code == 403


def test_theme_json_create_returns_shape_and_conflict(client):
    response = client.post(
        "/themes",
        json={"name": " AI "},
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 201
    assert response.json() == {"id": 1, "name": "AI"}
    original_id = response.json()["id"]

    duplicate = client.post(
        "/themes",
        json={"name": "ai"},
        headers=_json_csrf_headers(client),
    )
    assert duplicate.status_code == 409
    assert duplicate.json() == {
        "error": "Theme name already exists",
        "theme": {"id": original_id, "name": "AI"},
    }


def test_theme_json_rename_conflict_returns_existing_theme(client, _setup_db):
    original_id = _seed_theme(_setup_db, "AI")
    rename_id = _seed_theme(_setup_db, "Growth")

    response = client.post(
        f"/themes/{rename_id}/rename",
        json={"name": " ai "},
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": "Theme name already exists",
        "theme": {"id": original_id, "name": "AI"},
    }


def test_empty_theme_name_shows_required_flash_message(client, _setup_db):
    response = client.post(
        "/themes",
        data=csrf_form_data(client, {"name": "   "}),
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Theme name is required." in response.text
    assert "Theme name already exists." not in response.text
    db = _setup_db()
    try:
        assert db.query(Theme).count() == 0
    finally:
        db.close()


def test_add_form_lists_theme_picker(client, _setup_db):
    _seed_theme(_setup_db, "Semiconductors")

    response = client.get("/add")

    assert response.status_code == 200
    assert "Theme/Sector/Industry tags" in response.text
    assert "Semiconductors" in response.text
    assert 'data-theme-create-button' in response.text
    assert "/static/theme-tags.js" in response.text
    assert 'href="/portfolio/themes"' in response.text


def test_add_position_assigns_existing_and_inline_new_themes(client, _setup_db):
    existing_id = _seed_theme(_setup_db, "AI")
    data = {
        "ticker": "NVDA",
        "company_name": "NVIDIA Corp",
        "cost_basis": "100.00",
        "initial_purchase_date": "2025-01-15",
        "investment_type": "long-term",
        "notes": "",
        "theme_ids": [str(existing_id)],
        "new_theme_names": "Semiconductors",
    }

    with patch("app.main.get_market_data_api_key", return_value=None):
        response = client.post(
            "/add",
            data=csrf_form_data(client, data),
            follow_redirects=False,
        )

    assert response.status_code == 303
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.ticker == "NVDA").one()
        assert sorted(theme.name for theme in position.themes) == ["AI", "Semiconductors"]
    finally:
        db.close()


def test_add_position_rejects_cross_user_theme(client, _setup_db):
    other_theme_id = _seed_theme(_setup_db, "Other", user_id="other-user-id")
    data = {
        "ticker": "MSFT",
        "company_name": "Microsoft",
        "cost_basis": "100.00",
        "initial_purchase_date": "2025-01-15",
        "investment_type": "long-term",
        "notes": "",
        "theme_ids": [str(other_theme_id)],
    }

    with patch("app.main.get_market_data_api_key", return_value=None):
        response = client.post("/add", data=csrf_form_data(client, data))

    assert response.status_code == 400


def test_edit_position_updates_theme_selection(client, _setup_db):
    old_theme_id = _seed_theme(_setup_db, "Growth")
    second_old_theme_id = _seed_theme(_setup_db, "Software")
    new_theme_id = _seed_theme(_setup_db, "Energy")
    position_id = _seed_position(_setup_db)
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.id == position_id).one()
        position.themes = [
            db.query(Theme).filter(Theme.id == old_theme_id).one(),
            db.query(Theme).filter(Theme.id == second_old_theme_id).one(),
        ]
        db.commit()
    finally:
        db.close()

    edit_form = client.get(f"/edit/{position_id}")
    assert edit_form.status_code == 200
    assert 'value="{}"'.format(old_theme_id) in edit_form.text
    assert "Growth" in edit_form.text

    response = client.post(
        f"/edit/{position_id}",
        data=csrf_form_data(
            client,
            {
                "ticker": "AAPL",
                "company_name": "AAPL Inc.",
                "cost_basis": "100.00",
                "initial_purchase_date": "2025-01-15",
                "investment_type": "long-term",
                "current_price": "130.00",
                "notes": "",
                "theme_ids": [str(new_theme_id)],
            },
        ),
        follow_redirects=False,
    )

    assert response.status_code == 303
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.id == position_id).one()
        assert [theme.name for theme in position.themes] == ["Energy"]
    finally:
        db.close()


def test_js_less_new_theme_parser_accepts_commas_and_newlines(client, _setup_db):
    data = {
        "ticker": "AMD",
        "company_name": "Advanced Micro Devices",
        "cost_basis": "100.00",
        "initial_purchase_date": "2025-01-15",
        "investment_type": "long-term",
        "notes": "",
        "new_theme_names": "AI, Semiconductors\nGrowth",
    }

    with patch("app.main.get_market_data_api_key", return_value=None):
        response = client.post(
            "/add",
            data=csrf_form_data(client, data),
            follow_redirects=False,
        )

    assert response.status_code == 303
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.ticker == "AMD").one()
        assert sorted(theme.name for theme in position.themes) == [
            "AI",
            "Growth",
            "Semiconductors",
        ]
    finally:
        db.close()


def test_free_tier_user_can_assign_theme_to_position_under_limit(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "AI")
    data = {
        "ticker": "META",
        "company_name": "Meta Platforms",
        "cost_basis": "100.00",
        "initial_purchase_date": "2025-01-15",
        "investment_type": "long-term",
        "notes": "",
        "theme_ids": [str(theme_id)],
    }

    with patch("app.main.get_market_data_api_key", return_value=None):
        response = client.post(
            "/add",
            data=csrf_form_data(client, data),
            follow_redirects=False,
        )

    assert response.status_code == 303
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.ticker == "META").one()
        assert [theme.name for theme in position.themes] == ["AI"]
        assert db.query(User).filter(User.id == "test-user-id").one().tier == "free"
    finally:
        db.close()


def test_tier_limit_rerender_preserves_selected_themes(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "AI")
    for idx in range(5):
        _seed_position(_setup_db, ticker=f"T{idx}")
    data = {
        "ticker": "IBM",
        "company_name": "IBM",
        "cost_basis": "100.00",
        "initial_purchase_date": "2025-01-15",
        "investment_type": "long-term",
        "notes": "",
        "theme_ids": [str(theme_id)],
        "new_theme_names": "Mainframes",
    }

    with patch("app.main.get_market_data_api_key", return_value=None):
        response = client.post("/add", data=csrf_form_data(client, data))

    assert response.status_code == 200
    assert "5-ticker limit on the free tier" in response.text
    theme_checkbox = f'value="{theme_id}"'
    assert theme_checkbox in response.text
    assert response.text.index(theme_checkbox) < response.text.index("checked")
    assert 'value="Mainframes"' in response.text


def test_portfolio_themes_page_is_user_scoped_and_shows_verdicts(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "AI")
    other_theme_id = _seed_theme(_setup_db, "Other User Theme", user_id="other-user-id")
    position_id = _seed_position(_setup_db, ticker="NVDA")
    other_position_id = _seed_position(_setup_db, ticker="TSLA", user_id="other-user-id")
    db = _setup_db()
    try:
        theme = db.query(Theme).filter(Theme.id == theme_id).one()
        position = db.query(Position).filter(Position.id == position_id).one()
        position.themes = [theme]
        other_theme = db.query(Theme).filter(Theme.id == other_theme_id).one()
        other_position = db.query(Position).filter(Position.id == other_position_id).one()
        other_position.themes = [other_theme]
        db.commit()
    finally:
        db.close()

    response = client.get("/portfolio/themes")

    assert response.status_code == 200
    assert "AI" in response.text
    assert "NVDA" in response.text
    assert "verdict-trim" in response.text
    assert "Other User Theme" not in response.text
    assert "TSLA" not in response.text


def test_delete_unknown_theme_returns_404(client):
    response = client.post(
        "/themes/999/delete",
        data=csrf_form_data(client),
        follow_redirects=False,
    )

    assert response.status_code == 404


def test_theme_rename_and_delete_do_not_delete_positions(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "Old")
    position_id = _seed_position(_setup_db)
    db = _setup_db()
    try:
        theme = db.query(Theme).filter(Theme.id == theme_id).one()
        position = db.query(Position).filter(Position.id == position_id).one()
        position.themes = [theme]
        db.commit()
    finally:
        db.close()

    renamed = client.post(
        f"/themes/{theme_id}/rename",
        data=csrf_form_data(client, {"name": "New"}),
        follow_redirects=False,
    )
    assert renamed.status_code == 303

    deleted = client.post(
        f"/themes/{theme_id}/delete",
        data=csrf_form_data(client),
        follow_redirects=False,
    )
    assert deleted.status_code == 303

    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.id == position_id).one()
        assert position.themes == []
        assert db.query(Theme).filter(Theme.id == theme_id).first() is None
    finally:
        db.close()


def test_add_position_to_theme_via_drag_drop(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "AI")
    position_id = _seed_position(_setup_db, ticker="NVDA")

    response = client.post(
        f"/themes/{theme_id}/positions/{position_id}",
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}

    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.id == position_id).one()
        assert any(theme.id == theme_id for theme in position.themes)
    finally:
        db.close()


def test_add_position_to_theme_is_idempotent(client, _setup_db):
    """Adding the same position twice must not create a duplicate association."""
    theme_id = _seed_theme(_setup_db, "AI")
    position_id = _seed_position(_setup_db, ticker="NVDA")

    client.post(
        f"/themes/{theme_id}/positions/{position_id}",
        headers=_json_csrf_headers(client),
    )
    response = client.post(
        f"/themes/{theme_id}/positions/{position_id}",
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 200
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.id == position_id).one()
        assert len([t for t in position.themes if t.id == theme_id]) == 1
    finally:
        db.close()


def test_add_position_to_theme_preserves_other_tags(client, _setup_db):
    """Adding a position to a second theme must keep the first tag intact."""
    theme_a = _seed_theme(_setup_db, "AI")
    theme_b = _seed_theme(_setup_db, "Semiconductors")
    position_id = _seed_position(_setup_db, ticker="NVDA")

    # Tag to first theme via the existing set_position_themes path
    db = _setup_db()
    try:
        theme = db.query(Theme).filter(Theme.id == theme_a).one()
        position = db.query(Position).filter(Position.id == position_id).one()
        position.themes = [theme]
        db.commit()
    finally:
        db.close()

    # Drag/drop add to second theme
    response = client.post(
        f"/themes/{theme_b}/positions/{position_id}",
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 200
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.id == position_id).one()
        theme_ids = {t.id for t in position.themes}
        assert len(position.themes) == 2, "Expected exactly 2 theme associations (no duplicates)"
        assert theme_ids == {theme_a, theme_b}
    finally:
        db.close()


def test_add_position_to_unknown_theme_returns_404(client, _setup_db):
    position_id = _seed_position(_setup_db)

    response = client.post(
        f"/themes/999/positions/{position_id}",
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 404


def test_add_unknown_position_to_theme_returns_404(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "AI")

    response = client.post(
        f"/themes/{theme_id}/positions/999",
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 404


def test_remove_position_from_theme(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "AI")
    position_id = _seed_position(_setup_db, ticker="NVDA")
    db = _setup_db()
    try:
        theme = db.query(Theme).filter(Theme.id == theme_id).one()
        position = db.query(Position).filter(Position.id == position_id).one()
        position.themes = [theme]
        db.commit()
    finally:
        db.close()

    response = client.post(
        f"/themes/{theme_id}/positions/{position_id}/remove",
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.id == position_id).one()
        assert position.themes == []
    finally:
        db.close()


def test_remove_position_from_theme_keeps_other_tags(client, _setup_db):
    """Removing a position from one theme must not affect other theme tags."""
    theme_a = _seed_theme(_setup_db, "AI")
    theme_b = _seed_theme(_setup_db, "Semiconductors")
    position_id = _seed_position(_setup_db, ticker="NVDA")
    db = _setup_db()
    try:
        pos = db.query(Position).filter(Position.id == position_id).one()
        pos.themes = [
            db.query(Theme).filter(Theme.id == theme_a).one(),
            db.query(Theme).filter(Theme.id == theme_b).one(),
        ]
        db.commit()
    finally:
        db.close()

    response = client.post(
        f"/themes/{theme_a}/positions/{position_id}/remove",
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 200
    db = _setup_db()
    try:
        position = db.query(Position).filter(Position.id == position_id).one()
        assert [t.id for t in position.themes] == [theme_b]
    finally:
        db.close()


def test_remove_position_not_in_theme_returns_404(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "AI")
    position_id = _seed_position(_setup_db)

    response = client.post(
        f"/themes/{theme_id}/positions/{position_id}/remove",
        headers=_json_csrf_headers(client),
    )

    assert response.status_code == 404


def test_add_remove_require_csrf(client, _setup_db):
    theme_id = _seed_theme(_setup_db, "AI")
    position_id = _seed_position(_setup_db)

    for url in [
        f"/themes/{theme_id}/positions/{position_id}",
        f"/themes/{theme_id}/positions/{position_id}/remove",
    ]:
        response = client.post(
            url,
            headers={"accept": "application/json"},
        )
        assert response.status_code == 403


def test_portfolio_themes_board_renders_all_positions(client, _setup_db):
    """Board page must supply all_positions so the tray can render."""
    theme_id = _seed_theme(_setup_db, "AI")
    position_id = _seed_position(_setup_db, ticker="NVDA")

    response = client.get("/portfolio/themes")

    assert response.status_code == 200
    # Board layout elements
    assert "position-tray" in response.text
    assert "theme-heatmap-grid" in response.text
    # Position appears in tray
    assert "NVDA" in response.text
    # CSRF protection for drag/drop still present
    assert "csrf_token" in response.text
