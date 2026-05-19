"""Tests for the POST /add route — automatic price fetching from Alpha Vantage."""

import re
from datetime import date, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.alpha_vantage_client import DailyBar
from app.database import get_authenticated_uow, get_uow
from app.main import _market_service, app
from app.market_data.exceptions import MarketDataError
from app.models import Base, Position, StrategyRuleConfig, User
from app.unit_of_work import SqlAlchemyUnitOfWork
from tests.csrf_utils import csrf_form_data


@pytest.fixture(autouse=True)
def _setup_db():
    """Use an in-memory SQLite database for each test."""
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

    with patch("app.main._refresh_single_position_task", return_value=None):
        app.dependency_overrides[get_uow] = override_get_uow
        app.dependency_overrides[get_authenticated_uow] = override_get_authenticated_uow
        yield TestingSession
        app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app)


FORM_DATA_BASE = {
    "ticker": "AAPL",
    "company_name": "Apple Inc.",
    "cost_basis": "150.00",
    "initial_purchase_date": "2025-01-15",
    "investment_type": "long-term",
    "notes": "",
}


class TestAddPositionFetchesPrice:
    """POST /add should fetch the latest closing price from Alpha Vantage."""

    def test_fetches_price_when_api_key_configured(self, client):
        """When the API key is set and the fetch succeeds, current_price and
        daily cache fields should be populated from Alpha Vantage."""
        fake_bars = [
            DailyBar(date=date(2026, 4, 17), close=185.50),
            DailyBar(date=date(2026, 4, 16), close=183.00),
        ]

        with (
            patch("app.main.get_market_data_api_key", return_value="fake_key"),
            patch.object(_market_service, "fetch_daily_series", return_value=fake_bars) as mock_fetch,
        ):
            resp = client.post("/add", data=csrf_form_data(client, FORM_DATA_BASE), follow_redirects=False)

        assert resp.status_code == 303
        mock_fetch.assert_called_once_with("AAPL")

    def test_falls_back_to_zero_when_no_api_key(self, client):
        """When the API key is not configured, current_price should default to 0."""
        with patch("app.main.get_market_data_api_key", return_value=None):
            resp = client.post("/add", data=csrf_form_data(client, FORM_DATA_BASE), follow_redirects=False)

        assert resp.status_code == 303

    def test_falls_back_to_zero_on_fetch_error(self, client):
        """When the Alpha Vantage fetch raises an error, current_price
        should fall back to 0 and the position should still be created."""
        with (
            patch("app.main.get_market_data_api_key", return_value="fake_key"),
            patch.object(
                _market_service, "fetch_daily_series",
                side_effect=MarketDataError("boom"),
            ),
        ):
            resp = client.post("/add", data=csrf_form_data(client, FORM_DATA_BASE), follow_redirects=False)

        assert resp.status_code == 303

    def test_no_current_price_field_in_form(self, client):
        """The GET /add form should not contain a current_price input."""
        resp = client.get("/add")
        assert resp.status_code == 200
        assert "<title>Add Position — Stock Sentinel</title>" in resp.text
        assert 'rel="icon" href="/static/favicon.svg"' in resp.text
        assert 'aria-label="Stock Sentinel home"' in resp.text
        assert "Sell · Trim · Hold" in resp.text
        assert "<h1>Add Position</h1>" in resp.text
        assert "Capture a holding with the details needed to evaluate it against your rules." in resp.text
        assert 'name="current_price"' not in resp.text
        assert 'data-api-submit="true"' in resp.text
        assert "/static/refresh-status.js" in resp.text
        assert "/static/ticker-lookup.js" in resp.text
        assert 'class="form-legend"' in resp.text
        assert "Required field" in resp.text
        assert "Auto-filled from ticker lookup — edit if needed." in resp.text
        assert 'id="ticker_lookup_price"' in resp.text
        assert 'id="ticker_lookup_picker"' in resp.text
        assert 'id="add-position-submit"' in resp.text
        assert 'readonly' not in resp.text
        for label in (
            "Ticker",
            "Company Name",
            "Cost Basis ($)",
            "Purchase Date",
            "Investment Type",
        ):
            assert re.search(
                rf"{re.escape(label)}\s*<span class=\"required-indicator\"",
                resp.text,
            )


class TestAddPositionFormRemoved:
    """POST /add should not accept a current_price field from the form."""

    def test_extra_current_price_field_is_ignored(self, client):
        """If somehow current_price is submitted in the form, it should be
        ignored — the price comes from Alpha Vantage, not the user."""
        data = {**FORM_DATA_BASE, "current_price": "999.99"}
        with patch("app.main.get_market_data_api_key", return_value=None):
            resp = client.post("/add", data=csrf_form_data(client, data), follow_redirects=False)

        # Should still succeed (extra form fields are ignored by FastAPI)
        assert resp.status_code == 303


class TestAddPositionTierLimits:
    def _seed_positions(self, session_maker, count: int, user_id: str = "test-user-id"):
        db = session_maker()
        try:
            for idx in range(count):
                db.add(
                    Position(
                        ticker=f"T{idx}",
                        company_name=f"Ticker {idx}",
                        cost_basis=100.0,
                        initial_purchase_date=date(2025, 1, 1),
                        investment_type="long-term",
                        current_price=110.0,
                        user_id=user_id,
                    )
                )
            db.commit()
        finally:
            db.close()

    def _set_user_access(self, session_maker, **updates):
        db = session_maker()
        try:
            user = db.query(User).filter(User.id == "test-user-id").one()
            for key, value in updates.items():
                setattr(user, key, value)
            db.commit()
        finally:
            db.close()

    def test_free_user_cannot_add_sixth_ticker(self, client, _setup_db):
        self._seed_positions(_setup_db, 5)

        with patch("app.main.get_market_data_api_key", return_value=None):
            resp = client.post("/add", data=csrf_form_data(client, FORM_DATA_BASE))

        assert resp.status_code == 200
        assert "5-ticker limit on the free tier" in resp.text

    def test_full_access_user_can_add_sixth_ticker(self, client, _setup_db):
        self._seed_positions(_setup_db, 5)
        self._set_user_access(_setup_db, tier="full_access")

        with patch("app.main.get_market_data_api_key", return_value=None):
            resp = client.post("/add", data=csrf_form_data(client, FORM_DATA_BASE), follow_redirects=False)

        assert resp.status_code == 303

    def test_admin_user_can_add_sixth_ticker(self, client, _setup_db):
        self._seed_positions(_setup_db, 5)
        self._set_user_access(_setup_db, is_admin=True)

        with patch("app.main.get_market_data_api_key", return_value=None):
            resp = client.post("/add", data=csrf_form_data(client, FORM_DATA_BASE), follow_redirects=False)

        assert resp.status_code == 303
