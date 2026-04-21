"""Tests for the /api/lookup/{ticker} endpoint."""

import pytest
from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


class TestLookupRoute:
    def test_returns_company_name(self, mocker):
        mocker.patch("app.main.get_alpha_vantage_api_key", return_value="fake_key")
        mocker.patch("app.main.fetch_company_name", return_value="Apple Inc")

        resp = client.get("/api/lookup/AAPL")
        assert resp.status_code == 200
        assert resp.json() == {"company_name": "Apple Inc"}

    def test_returns_503_when_no_api_key(self, mocker):
        mocker.patch("app.main.get_alpha_vantage_api_key", return_value=None)

        resp = client.get("/api/lookup/AAPL")
        assert resp.status_code == 503
        assert "error" in resp.json()

    def test_returns_502_on_alpha_vantage_error(self, mocker):
        from app.alpha_vantage_client import AlphaVantageSymbolNotFound

        mocker.patch("app.main.get_alpha_vantage_api_key", return_value="fake_key")
        mocker.patch(
            "app.main.fetch_company_name",
            side_effect=AlphaVantageSymbolNotFound("No matching company"),
        )

        resp = client.get("/api/lookup/INVALID")
        assert resp.status_code == 502
        assert "error" in resp.json()

    def test_ticker_is_uppercased_and_stripped(self, mocker):
        mocker.patch("app.main.get_alpha_vantage_api_key", return_value="fake_key")
        mock_fetch = mocker.patch("app.main.fetch_company_name", return_value="Apple Inc")

        client.get("/api/lookup/ aapl ")
        mock_fetch.assert_called_once_with("AAPL", "fake_key")

    def test_returns_502_on_connection_error(self, mocker):
        mocker.patch("app.main.get_alpha_vantage_api_key", return_value="fake_key")
        mocker.patch(
            "app.main.fetch_company_name",
            side_effect=ConnectionError("Failed to resolve host"),
        )

        resp = client.get("/api/lookup/AAPL")
        assert resp.status_code == 502
        assert "error" in resp.json()
