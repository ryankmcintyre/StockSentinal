"""Tests for the Alpha Vantage client response parsing."""

from datetime import date

import pytest

from app.alpha_vantage_client import (
    AlphaVantageError,
    AlphaVantageSymbolNotFound,
    AlphaVantageThrottled,
    DailyBar,
    SMAPoint,
    WeeklyBar,
    _get,
    fetch_daily_series,
    fetch_sma,
    fetch_weekly_series,
)


# ---------------------------------------------------------------------------
# _get error handling
# ---------------------------------------------------------------------------


class TestGetErrorHandling:
    def test_throttle_detection(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "Note": "Thank you for using Alpha Vantage! API call frequency limit reached."
        }
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.alpha_vantage_client.requests.get", return_value=mock_resp)

        with pytest.raises(AlphaVantageThrottled):
            _get({"function": "TIME_SERIES_DAILY", "symbol": "IBM"}, "fake_key")

    def test_symbol_not_found(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "Error Message": "Invalid API call. Please retry or visit..."
        }
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.alpha_vantage_client.requests.get", return_value=mock_resp)

        with pytest.raises(AlphaVantageSymbolNotFound):
            _get({"function": "TIME_SERIES_DAILY", "symbol": "INVALID"}, "fake_key")


# ---------------------------------------------------------------------------
# Daily series parsing
# ---------------------------------------------------------------------------


class TestFetchDailySeries:
    def test_parses_bars(self, mocker):
        fake_data = {
            "Meta Data": {"2. Symbol": "IBM"},
            "Time Series (Daily)": {
                "2026-04-17": {
                    "1. open": "180.00",
                    "2. high": "182.00",
                    "3. low": "179.00",
                    "4. close": "181.50",
                    "5. volume": "3000000",
                },
                "2026-04-16": {
                    "1. open": "178.00",
                    "2. high": "180.00",
                    "3. low": "177.00",
                    "4. close": "179.00",
                    "5. volume": "2500000",
                },
            },
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.alpha_vantage_client.requests.get", return_value=mock_resp)

        bars = fetch_daily_series("IBM", "fake_key")
        assert len(bars) == 2
        assert bars[0].date == date(2026, 4, 17)
        assert bars[0].close == 181.50
        assert bars[1].date == date(2026, 4, 16)

    def test_raises_on_missing_key(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"Meta Data": {}}
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.alpha_vantage_client.requests.get", return_value=mock_resp)

        with pytest.raises(AlphaVantageError, match="missing"):
            fetch_daily_series("IBM", "fake_key")


# ---------------------------------------------------------------------------
# Weekly series parsing
# ---------------------------------------------------------------------------


class TestFetchWeeklySeries:
    def test_parses_bars(self, mocker):
        fake_data = {
            "Meta Data": {"2. Symbol": "IBM"},
            "Weekly Time Series": {
                "2026-04-17": {
                    "1. open": "175.00",
                    "2. high": "185.00",
                    "3. low": "174.00",
                    "4. close": "182.00",
                    "5. volume": "15000000",
                },
                "2026-04-10": {
                    "1. open": "170.00",
                    "2. high": "178.00",
                    "3. low": "169.00",
                    "4. close": "176.00",
                    "5. volume": "14000000",
                },
            },
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.alpha_vantage_client.requests.get", return_value=mock_resp)

        bars = fetch_weekly_series("IBM", "fake_key")
        assert len(bars) == 2
        assert bars[0].date == date(2026, 4, 17)
        assert bars[0].close == 182.00


# ---------------------------------------------------------------------------
# SMA parsing
# ---------------------------------------------------------------------------


class TestFetchSMA:
    def test_parses_sma_points(self, mocker):
        fake_data = {
            "Meta Data": {},
            "Technical Analysis: SMA": {
                "2026-04-17": {"SMA": "178.25"},
                "2026-04-16": {"SMA": "177.80"},
            },
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.alpha_vantage_client.requests.get", return_value=mock_resp)

        points = fetch_sma("IBM", "daily", 21, "fake_key")
        assert len(points) == 2
        assert points[0].date == date(2026, 4, 17)
        assert points[0].sma == 178.25

    def test_raises_on_missing_key(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"Meta Data": {}}
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.alpha_vantage_client.requests.get", return_value=mock_resp)

        with pytest.raises(AlphaVantageError, match="missing"):
            fetch_sma("IBM", "daily", 21, "fake_key")
