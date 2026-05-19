"""Tests for the /api/lookup/{ticker} endpoint."""

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.alpha_vantage_client import DailyBar, SymbolSearchMatch
from app.database import get_authenticated_uow, get_uow
from app.main import _market_service, app
from app.market_data.exceptions import MarketDataError, MarketDataSymbolNotFound
from app.models import Base, Position, StrategyRuleConfig, User
from app.unit_of_work import SqlAlchemyUnitOfWork


TEST_USER_ID = "test-user-id"
TEST_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _setup_db(monkeypatch):
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
                obj.id = TEST_USER_ID
            if isinstance(obj, Position) and obj.user_id is None:
                obj.user_id = TEST_USER_ID
            if isinstance(obj, StrategyRuleConfig) and obj.user_id is None:
                obj.user_id = TEST_USER_ID

    db = TestingSession()
    db.add(
        User(
            id=TEST_USER_ID,
            email="test@example.com",
            display_name="Test User",
            created_at=TEST_NOW,
        )
    )
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
            yield SqlAlchemyUnitOfWork(session, user_id=TEST_USER_ID)
        finally:
            session.close()

    monkeypatch.setattr("app.main.SessionLocal", TestingSession)
    monkeypatch.setattr("app.main.init_db", lambda: None)
    app.dependency_overrides[get_uow] = override_get_uow
    app.dependency_overrides[get_authenticated_uow] = override_get_authenticated_uow
    yield
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def authenticated_client():
    with TestClient(app) as client:
        yield client


class TestLookupRoute:
    def test_anonymous_requests_redirect_to_login(self):
        authenticated_uow_override = app.dependency_overrides.pop(
            get_authenticated_uow, None
        )
        try:
            with TestClient(app) as client:
                resp = client.get("/api/lookup/AAPL", follow_redirects=False)
        finally:
            if authenticated_uow_override is not None:
                app.dependency_overrides[get_authenticated_uow] = (
                    authenticated_uow_override
                )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/auth/login"

    def test_returns_matches_and_price(self, authenticated_client, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        mocker.patch.object(
            _market_service,
            "fetch_ticker_matches",
            return_value=[
                SymbolSearchMatch(
                    symbol="AAPL",
                    name="Apple Inc",
                    region="United States",
                    type="Equity",
                    match_score=1.0,
                )
            ],
        )
        mocker.patch.object(
            _market_service,
            "fetch_daily_series",
            return_value=[DailyBar(date=date(2026, 4, 17), close=182.45)],
        )

        resp = authenticated_client.get("/api/lookup/AAPL")

        assert resp.status_code == 200
        assert resp.json() == {
            "company_name": "Apple Inc",
            "current_price": 182.45,
            "matches": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc",
                    "region": "United States",
                    "type": "Equity",
                    "match_score": 1.0,
                }
            ],
        }

    def test_returns_503_when_no_api_key(self, authenticated_client, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value=None)

        resp = authenticated_client.get("/api/lookup/AAPL")

        assert resp.status_code == 503
        assert "error" in resp.json()

    def test_returns_404_when_no_matches_found(self, authenticated_client, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        mocker.patch.object(
            _market_service,
            "fetch_ticker_matches",
            side_effect=MarketDataSymbolNotFound("No matching company"),
        )

        resp = authenticated_client.get("/api/lookup/INVALID")

        assert resp.status_code == 404
        assert resp.json() == {"error": "No results found for INVALID"}

    def test_ticker_is_uppercased_and_stripped(self, authenticated_client, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        mock_fetch_matches = mocker.patch.object(
            _market_service,
            "fetch_ticker_matches",
            return_value=[SymbolSearchMatch(symbol="AAPL", name="Apple Inc")],
        )
        mock_fetch_price = mocker.patch.object(
            _market_service,
            "fetch_daily_series",
            return_value=[],
        )

        authenticated_client.get("/api/lookup/ aapl ")

        mock_fetch_matches.assert_called_once_with("AAPL")
        mock_fetch_price.assert_called_once_with("AAPL")

    def test_returns_502_on_connection_error(self, authenticated_client, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        mocker.patch.object(
            _market_service,
            "fetch_ticker_matches",
            side_effect=ConnectionError("Failed to resolve host"),
        )

        resp = authenticated_client.get("/api/lookup/AAPL")

        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_hides_price_when_price_lookup_fails(self, authenticated_client, mocker):
        mocker.patch("app.main.get_market_data_api_key", return_value="fake_key")
        mocker.patch.object(
            _market_service,
            "fetch_ticker_matches",
            return_value=[SymbolSearchMatch(symbol="AAPL", name="Apple Inc")],
        )
        mocker.patch.object(
            _market_service,
            "fetch_daily_series",
            side_effect=MarketDataError("price unavailable"),
        )

        resp = authenticated_client.get("/api/lookup/AAPL")

        assert resp.status_code == 200
        assert resp.json()["current_price"] is None
