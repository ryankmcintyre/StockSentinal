"""Tests for the POST /add route — automatic price fetching from Alpha Vantage."""

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.alpha_vantage_client import AlphaVantageError, DailyBar
from app.database import get_uow
from app.main import _market_service, app
from app.models import Base
from app.unit_of_work import SqlAlchemyUnitOfWork


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

    def override_get_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session)
        finally:
            session.close()

    with patch("app.main._refresh_single_position_task", return_value=None):
        app.dependency_overrides[get_uow] = override_get_uow
        yield
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
            patch("app.main.get_alpha_vantage_api_key", return_value="fake_key"),
            patch.object(_market_service, "fetch_daily_series", return_value=fake_bars) as mock_fetch,
        ):
            resp = client.post("/add", data=FORM_DATA_BASE, follow_redirects=False)

        assert resp.status_code == 303
        mock_fetch.assert_called_once_with("AAPL")

    def test_falls_back_to_zero_when_no_api_key(self, client):
        """When the API key is not configured, current_price should default to 0."""
        with patch("app.main.get_alpha_vantage_api_key", return_value=None):
            resp = client.post("/add", data=FORM_DATA_BASE, follow_redirects=False)

        assert resp.status_code == 303

    def test_falls_back_to_zero_on_fetch_error(self, client):
        """When the Alpha Vantage fetch raises an error, current_price
        should fall back to 0 and the position should still be created."""
        with (
            patch("app.main.get_alpha_vantage_api_key", return_value="fake_key"),
            patch.object(
                _market_service, "fetch_daily_series",
                side_effect=AlphaVantageError("boom"),
            ),
        ):
            resp = client.post("/add", data=FORM_DATA_BASE, follow_redirects=False)

        assert resp.status_code == 303

    def test_no_current_price_field_in_form(self, client):
        """The GET /add form should not contain a current_price input."""
        resp = client.get("/add")
        assert resp.status_code == 200
        assert 'name="current_price"' not in resp.text
        assert 'data-api-submit="true"' in resp.text
        assert "/static/refresh-status.js" in resp.text


class TestAddPositionFormRemoved:
    """POST /add should not accept a current_price field from the form."""

    def test_extra_current_price_field_is_ignored(self, client):
        """If somehow current_price is submitted in the form, it should be
        ignored — the price comes from Alpha Vantage, not the user."""
        data = {**FORM_DATA_BASE, "current_price": "999.99"}
        with patch("app.main.get_alpha_vantage_api_key", return_value=None):
            resp = client.post("/add", data=data, follow_redirects=False)

        # Should still succeed (extra form fields are ignored by FastAPI)
        assert resp.status_code == 303
