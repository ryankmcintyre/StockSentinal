"""Tests for the key-level CRUD routes (issue #23)."""

import re
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_authenticated_uow, get_uow
from app.main import app
from app.models import Base, Position, PositionKeyLevel, StrategyRuleConfig, User
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

    @event.listens_for(TestingSession.class_, "before_flush")
    def _assign_test_user_id(session, _flush_context, _instances):
        for obj in session.new:
            if isinstance(obj, User) and not obj.id:
                obj.id = "test-user-id"
            if isinstance(obj, Position) and obj.user_id is None:
                obj.user_id = "test-user-id"
            if isinstance(obj, StrategyRuleConfig) and obj.user_id is None:
                obj.user_id = "test-user-id"

    db = TestingSession()
    db.add(User(id="test-user-id", email="test@example.com", display_name="Test User", created_at=datetime.now()))
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

    app.dependency_overrides[get_uow] = override_get_uow
    app.dependency_overrides[get_authenticated_uow] = override_get_authenticated_uow
    yield TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app)


def _seed_position(SessionMaker, **overrides) -> int:
    db = SessionMaker()
    try:
        values = {
            "ticker": "NVDA",
            "company_name": "NVIDIA Corp",
            "cost_basis": 100.0,
            "initial_purchase_date": date(2024, 1, 1),
            "investment_type": "long-term",
            "current_price": 150.0,
        }
        values.update(overrides)
        pos = Position(
            **values,
        )
        db.add(pos)
        db.commit()
        return pos.id
    finally:
        db.close()


class TestKeyLevelRoutes:
    def test_add_key_level(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        resp = client.post(
            f"/edit/{pos_id}/key-levels/add",
            data={"level_price": "120.5", "label": "2024 high", "notes": "from chart"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/edit/{pos_id}"

        db = _setup_db()
        try:
            kls = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.position_id == pos_id
            ).all()
            assert len(kls) == 1
            assert kls[0].level_price == 120.5
            assert kls[0].label == "2024 high"
            assert kls[0].notes == "from chart"
            assert kls[0].is_active is True
        finally:
            db.close()

    def test_add_rejects_non_positive_price(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        resp = client.post(
            f"/edit/{pos_id}/key-levels/add",
            data={"level_price": "0", "label": "bad"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db = _setup_db()
        try:
            kls = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.position_id == pos_id
            ).all()
            assert kls == []
        finally:
            db.close()

    def test_add_to_unknown_position_redirects_to_root(self, _setup_db, client):
        resp = client.post(
            "/edit/99999/key-levels/add",
            data={"level_price": "100"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_delete_key_level(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            kl = PositionKeyLevel(position_id=pos_id, level_price=100.0)
            db.add(kl)
            db.commit()
            kl_id = kl.id
        finally:
            db.close()

        resp = client.post(
            f"/edit/{pos_id}/key-levels/{kl_id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = _setup_db()
        try:
            assert db.query(PositionKeyLevel).filter(
                PositionKeyLevel.id == kl_id
            ).first() is None
        finally:
            db.close()

    def test_toggle_key_level_active(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            kl = PositionKeyLevel(position_id=pos_id, level_price=100.0, is_active=True)
            db.add(kl)
            db.commit()
            kl_id = kl.id
        finally:
            db.close()

        client.post(f"/edit/{pos_id}/key-levels/{kl_id}/toggle", follow_redirects=False)

        db = _setup_db()
        try:
            kl = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.id == kl_id
            ).first()
            assert kl.is_active is False
        finally:
            db.close()

        client.post(f"/edit/{pos_id}/key-levels/{kl_id}/toggle", follow_redirects=False)

        db = _setup_db()
        try:
            kl = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.id == kl_id
            ).first()
            assert kl.is_active is True
        finally:
            db.close()

    def test_delete_key_level_for_other_user_is_noop(self, _setup_db, client):
        db = _setup_db()
        try:
            db.add(User(id="alice-user-id", email="alice@example.com"))
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2024, 1, 1),
                investment_type="long-term",
                current_price=150.0,
                user_id="alice-user-id",
            )
            db.add(pos)
            db.commit()
            kl = PositionKeyLevel(position_id=pos.id, level_price=100.0)
            db.add(kl)
            db.commit()
            pos_id = pos.id
            kl_id = kl.id
        finally:
            db.close()

        resp = client.post(
            f"/edit/{pos_id}/key-levels/{kl_id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = _setup_db()
        try:
            assert db.query(PositionKeyLevel).filter(
                PositionKeyLevel.id == kl_id
            ).first() is not None
        finally:
            db.close()

    def test_toggle_key_level_for_other_user_is_noop(self, _setup_db, client):
        db = _setup_db()
        try:
            db.add(User(id="alice-user-id", email="alice@example.com"))
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2024, 1, 1),
                investment_type="long-term",
                current_price=150.0,
                user_id="alice-user-id",
            )
            db.add(pos)
            db.commit()
            kl = PositionKeyLevel(position_id=pos.id, level_price=100.0, is_active=True)
            db.add(kl)
            db.commit()
            pos_id = pos.id
            kl_id = kl.id
        finally:
            db.close()

        resp = client.post(
            f"/edit/{pos_id}/key-levels/{kl_id}/toggle",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = _setup_db()
        try:
            kl = db.query(PositionKeyLevel).filter(
                PositionKeyLevel.id == kl_id
            ).first()
            assert kl.is_active is True
        finally:
            db.close()

    def test_delete_key_level_requires_authentication(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            kl = PositionKeyLevel(position_id=pos_id, level_price=100.0)
            db.add(kl)
            db.commit()
            kl_id = kl.id
        finally:
            db.close()

        original_override = app.dependency_overrides.pop(get_authenticated_uow)
        try:
            resp = client.post(
                f"/edit/{pos_id}/key-levels/{kl_id}/delete",
                follow_redirects=False,
            )
        finally:
            app.dependency_overrides[get_authenticated_uow] = original_override

        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_toggle_key_level_requires_authentication(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            kl = PositionKeyLevel(position_id=pos_id, level_price=100.0)
            db.add(kl)
            db.commit()
            kl_id = kl.id
        finally:
            db.close()

        original_override = app.dependency_overrides.pop(get_authenticated_uow)
        try:
            resp = client.post(
                f"/edit/{pos_id}/key-levels/{kl_id}/toggle",
                follow_redirects=False,
            )
        finally:
            app.dependency_overrides[get_authenticated_uow] = original_override

        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_delete_position_cascades_to_key_levels(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            db.add_all([
                PositionKeyLevel(position_id=pos_id, level_price=100.0),
                PositionKeyLevel(position_id=pos_id, level_price=120.0),
            ])
            db.commit()
        finally:
            db.close()

        client.post(f"/delete/{pos_id}", follow_redirects=False)

        db = _setup_db()
        try:
            assert db.query(PositionKeyLevel).filter(
                PositionKeyLevel.position_id == pos_id
            ).all() == []
        finally:
            db.close()

    def test_edit_position_renders_key_levels_section(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)
        db = _setup_db()
        try:
            db.add(PositionKeyLevel(position_id=pos_id, level_price=120.0, label="LTH"))
            db.commit()
        finally:
            db.close()

        resp = client.get(f"/edit/{pos_id}")
        assert resp.status_code == 200
        assert "Key Levels" in resp.text
        assert "120.00" in resp.text
        assert "LTH" in resp.text

    def test_edit_position_marks_required_fields(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)

        resp = client.get(f"/edit/{pos_id}")

        assert resp.status_code == 200
        assert 'class="form-legend"' in resp.text
        assert "Required field" in resp.text
        for label in (
            "Cost Basis ($)",
            "Current Price ($)",
            "Purchase Date",
            "Investment Type",
        ):
            assert re.search(
                rf"{re.escape(label)}\s*<span class=\"required-indicator\"",
                resp.text,
            )

    def test_edit_position_shows_ticker_lookup_fields(self, _setup_db, client):
        pos_id = _seed_position(_setup_db)

        resp = client.get(f"/edit/{pos_id}")

        assert resp.status_code == 200
        assert 'name="ticker"' in resp.text
        assert 'value="NVDA"' in resp.text
        assert 'name="company_name"' in resp.text
        assert 'value="NVIDIA Corp"' in resp.text
        assert 'id="ticker_lookup_status"' in resp.text
        assert 'id="ticker_lookup_price"' in resp.text
        assert 'id="ticker_lookup_picker"' in resp.text
        assert "/static/ticker-lookup.js" in resp.text
        assert "Auto-filled from ticker lookup — edit if needed." in resp.text
        assert 'id="edit-position-submit"' in resp.text

    def test_edit_position_clears_cached_data_on_ticker_change(
        self, _setup_db, client, mocker
    ):
        mocker.patch("app.main.get_market_data_api_key", return_value=None)
        pos_id = _seed_position(
            _setup_db,
            daily_close=151.0,
            daily_sma_21=149.5,
            daily_market_date=date(2024, 3, 1),
            daily_retrieved_at=datetime(2024, 3, 1, 10, 30),
            weekly_close=148.0,
            weekly_sma_20=145.0,
            weekly_market_date=date(2024, 2, 23),
            weekly_retrieved_at=datetime(2024, 2, 23, 16, 0),
            refresh_error="old error",
        )

        resp = client.post(
            f"/edit/{pos_id}",
            data={
                "ticker": " msft ",
                "company_name": " Microsoft Corporation ",
                "cost_basis": "110.00",
                "initial_purchase_date": "2024-02-01",
                "investment_type": "short-term",
                "current_price": "125.50",
                "notes": " updated notes ",
                "sector_benchmark_ticker": " xlk ",
            },
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        db = _setup_db()
        try:
            pos = db.query(Position).filter(Position.id == pos_id).first()
            assert pos.ticker == "MSFT"
            assert pos.company_name == "Microsoft Corporation"
            assert pos.cost_basis == 110.0
            assert pos.initial_purchase_date == date(2024, 2, 1)
            assert pos.investment_type == "short-term"
            assert pos.current_price == 125.5
            assert pos.notes == "updated notes"
            assert pos.sector_benchmark_ticker == "XLK"
            assert pos.daily_close is None
            assert pos.daily_sma_21 is None
            assert pos.daily_market_date is None
            assert pos.daily_retrieved_at is None
            assert pos.weekly_close is None
            assert pos.weekly_sma_20 is None
            assert pos.weekly_market_date is None
            assert pos.weekly_retrieved_at is None
            assert pos.refresh_error is None
        finally:
            db.close()

    def test_edit_position_schedules_refresh_when_ticker_changes_with_api_key(
        self, _setup_db, client, mocker
    ):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        mock_refresh = mocker.patch("app.main._refresh_single_position_task", return_value=None)
        pos_id = _seed_position(_setup_db)

        resp = client.post(
            f"/edit/{pos_id}",
            data={
                "ticker": "msft",
                "company_name": "Microsoft Corporation",
                "cost_basis": "100.00",
                "initial_purchase_date": "2024-01-01",
                "investment_type": "long-term",
                "current_price": "150.00",
                "notes": "",
                "sector_benchmark_ticker": "",
            },
            follow_redirects=False,
        )

        assert resp.status_code == 303
        mock_refresh.assert_called_once_with(pos_id)
