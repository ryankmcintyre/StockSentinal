"""Tests for the /api/lookup/{ticker} endpoint."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.alpha_vantage_client import DailyBar, SymbolSearchMatch
from app.database import get_authenticated_uow
from app.main import _market_service, app
from app.market_data.exceptions import MarketDataError, MarketDataSymbolNotFound


@pytest.fixture()
def authenticated_client():
    def mock_authenticated_uow():
        yield None

    app.dependency_overrides[get_authenticated_uow] = mock_authenticated_uow
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


class TestLookupRoute:
    def test_anonymous_requests_redirect_to_login(self):
        with TestClient(app) as client:
            resp = client.get("/api/lookup/AAPL", follow_redirects=False)

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
