"""Tests for the Twelve Data client response parsing."""

from datetime import date

import pytest

from app.alpha_vantage_client import ATRPoint, DailyBar, SMAPoint, WeeklyBar
from app.market_data.exceptions import (
    MarketDataError,
    MarketDataSymbolNotFound,
    MarketDataThrottled,
)
from app.twelve_data_client import (
    _get,
    fetch_atr,
    fetch_atr_batch,
    fetch_company_name,
    fetch_daily_series_batch,
    fetch_ticker_matches,
    fetch_daily_series,
    fetch_sma,
    fetch_weekly_series_batch,
    fetch_weekly_series,
)


class TestGetErrorHandling:
    def test_throttle_detection(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "error",
            "code": 429,
            "message": "API credits exhausted for the minute",
        }
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        with pytest.raises(MarketDataThrottled):
            _get("/time_series", {"symbol": "IBM", "interval": "1day"}, "fake_key")

    def test_symbol_not_found(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "status": "error",
            "code": 400,
            "message": "Symbol not found: INVALID",
        }
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        with pytest.raises(MarketDataSymbolNotFound):
            _get("/time_series", {"symbol": "INVALID", "interval": "1day"}, "fake_key")


class TestFetchDailySeries:
    def test_parses_bars(self, mocker):
        fake_data = {
            "meta": {"symbol": "IBM"},
            "values": [
                {"datetime": "2026-04-16", "close": "179.00"},
                {"datetime": "2026-04-17", "close": "181.50"},
            ],
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        bars = fetch_daily_series("IBM", "fake_key")
        assert bars == [
            DailyBar(date=date(2026, 4, 17), close=181.50),
            DailyBar(date=date(2026, 4, 16), close=179.00),
        ]

    def test_raises_on_missing_values(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"meta": {"symbol": "IBM"}}
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        with pytest.raises(MarketDataSymbolNotFound, match="No time series data"):
            fetch_daily_series("IBM", "fake_key")


class TestFetchDailySeriesBatch:
    def test_parses_batch_bars_from_single_http_request(self, mocker):
        fake_data = {
            "AAPL": {
                "meta": {"symbol": "AAPL"},
                "values": [
                    {"datetime": "2026-04-16", "close": "179.00"},
                    {"datetime": "2026-04-17", "close": "181.50"},
                ],
            },
            "MSFT": {
                "meta": {"symbol": "MSFT"},
                "values": [
                    {"datetime": "2026-04-17", "close": "425.25"},
                ],
            },
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mock_get = mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        bars_by_symbol = fetch_daily_series_batch(
            [" aapl ", "MSFT", "AAPL", "", "   "],
            "fake_key",
        )

        assert mock_get.call_count == 1
        assert mock_get.call_args.kwargs["params"]["symbol"] == "AAPL,MSFT"
        assert mock_get.call_args.kwargs["params"]["interval"] == "1day"
        assert bars_by_symbol["AAPL"] == [
            DailyBar(date=date(2026, 4, 17), close=181.50),
            DailyBar(date=date(2026, 4, 16), close=179.00),
        ]
        assert bars_by_symbol["MSFT"] == [
            DailyBar(date=date(2026, 4, 17), close=425.25),
        ]

    def test_skips_symbol_level_errors(self, mocker):
        fake_data = {
            "AAPL": {
                "meta": {"symbol": "AAPL"},
                "values": [{"datetime": "2026-04-17", "close": "181.50"}],
            },
            "ZZZZ": {
                "status": "error",
                "code": 400,
                "message": "Symbol not found: ZZZZ",
            },
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        bars_by_symbol = fetch_daily_series_batch(["AAPL", "ZZZZ"], "fake_key")

        assert set(bars_by_symbol) == {"AAPL"}


class TestFetchWeeklySeries:
    def test_parses_bars(self, mocker):
        fake_data = {
            "meta": {"symbol": "IBM"},
            "values": [
                {
                    "datetime": "2026-04-10",
                    "open": "170.00",
                    "high": "178.00",
                    "low": "169.00",
                    "close": "176.00",
                    "volume": "14000000",
                },
                {
                    "datetime": "2026-04-17",
                    "open": "175.00",
                    "high": "185.00",
                    "low": "174.00",
                    "close": "182.00",
                    "volume": "15000000",
                },
            ],
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        bars = fetch_weekly_series("IBM", "fake_key")
        assert bars == [
            WeeklyBar(
                date=date(2026, 4, 17),
                open=175.00,
                high=185.00,
                low=174.00,
                close=182.00,
                volume=15000000.0,
            ),
            WeeklyBar(
                date=date(2026, 4, 10),
                open=170.00,
                high=178.00,
                low=169.00,
                close=176.00,
                volume=14000000.0,
            ),
        ]


class TestFetchWeeklySeriesBatch:
    def test_parses_batch_bars(self, mocker):
        fake_data = {
            "IBM": {
                "meta": {"symbol": "IBM"},
                "values": [
                    {
                        "datetime": "2026-04-17",
                        "open": "175.00",
                        "high": "185.00",
                        "low": "174.00",
                        "close": "182.00",
                        "volume": "15000000",
                    },
                ],
            },
            "AAPL": {
                "meta": {"symbol": "AAPL"},
                "values": [
                    {
                        "datetime": "2026-04-17",
                        "open": "200.00",
                        "high": "210.00",
                        "low": "199.00",
                        "close": "205.00",
                        "volume": "25000000",
                    },
                ],
            },
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mock_get = mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        bars_by_symbol = fetch_weekly_series_batch(["IBM", "AAPL"], "fake_key")

        assert mock_get.call_count == 1
        assert mock_get.call_args.kwargs["params"]["symbol"] == "IBM,AAPL"
        assert mock_get.call_args.kwargs["params"]["interval"] == "1week"
        assert bars_by_symbol["IBM"] == [
            WeeklyBar(
                date=date(2026, 4, 17),
                open=175.00,
                high=185.00,
                low=174.00,
                close=182.00,
                volume=15000000.0,
            )
        ]


class TestFetchSma:
    def test_parses_points_and_maps_interval(self, mocker):
        fake_data = {
            "meta": {"symbol": "IBM"},
            "values": [
                {"datetime": "2026-04-16", "sma": "177.80"},
                {"datetime": "2026-04-17", "sma": "178.25"},
            ],
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mock_get = mocker.patch(
            "app.twelve_data_client.requests.get", return_value=mock_resp
        )

        points = fetch_sma("IBM", "daily", 21, "fake_key")
        assert points == [
            SMAPoint(date=date(2026, 4, 17), sma=178.25),
            SMAPoint(date=date(2026, 4, 16), sma=177.80),
        ]
        assert mock_get.call_args.kwargs["params"]["interval"] == "1day"

    def test_raises_on_missing_values(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"meta": {"symbol": "IBM"}}
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        with pytest.raises(MarketDataError, match="missing 'values'"):
            fetch_sma("IBM", "daily", 21, "fake_key")


class TestFetchAtr:
    def test_parses_points_sorted_most_recent_first(self, mocker):
        fake_data = {
            "meta": {"symbol": "IBM"},
            "values": [
                {"datetime": "2026-04-15", "atr": "2.10"},
                {"datetime": "2026-04-17", "atr": "2.50"},
                {"datetime": "2026-04-16", "atr": "2.30"},
            ],
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        points = fetch_atr("IBM", "weekly", 14, "fake_key")
        assert points == [
            ATRPoint(date=date(2026, 4, 17), atr=2.50),
            ATRPoint(date=date(2026, 4, 16), atr=2.30),
            ATRPoint(date=date(2026, 4, 15), atr=2.10),
        ]

    def test_batch_parses_multiple_symbols_in_single_request(self, mocker):
        fake_data = {
            "AAPL": {
                "meta": {"symbol": "AAPL"},
                "values": [{"datetime": "2026-04-17", "atr": "3.10"}],
            },
            "MSFT": {
                "meta": {"symbol": "MSFT"},
                "values": [{"datetime": "2026-04-17", "atr": "4.20"}],
            },
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mock_get = mocker.patch(
            "app.twelve_data_client.requests.get", return_value=mock_resp
        )

        atr_by_symbol = fetch_atr_batch(
            [" aapl ", "MSFT", "AAPL"], "daily", 14, "fake_key"
        )

        assert mock_get.call_count == 1
        assert mock_get.call_args.kwargs["params"]["symbol"] == "AAPL,MSFT"
        assert mock_get.call_args.kwargs["params"]["interval"] == "1day"
        assert mock_get.call_args.kwargs["params"]["time_period"] == "14"
        assert atr_by_symbol["AAPL"] == [ATRPoint(date=date(2026, 4, 17), atr=3.10)]
        assert atr_by_symbol["MSFT"] == [ATRPoint(date=date(2026, 4, 17), atr=4.20)]

    def test_batch_skips_symbol_level_errors(self, mocker):
        fake_data = {
            "AAPL": {
                "meta": {"symbol": "AAPL"},
                "values": [{"datetime": "2026-04-17", "atr": "3.10"}],
            },
            "ZZZZ": {
                "status": "error",
                "code": 400,
                "message": "Symbol not found: ZZZZ",
            },
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        atr_by_symbol = fetch_atr_batch(["AAPL", "ZZZZ"], "daily", 14, "fake_key")

        assert set(atr_by_symbol) == {"AAPL"}


class TestFetchTickerMatches:
    def test_returns_ticker_matches(self, mocker):
        fake_data = {
            "data": [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc",
                    "country": "United States",
                    "type": "Common Stock",
                },
            ]
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        matches = fetch_ticker_matches("AAPL", "fake_key")

        assert len(matches) == 1
        assert matches[0].symbol == "AAPL"
        assert matches[0].name == "Apple Inc"
        assert matches[0].region == "United States"


class TestFetchCompanyName:
    def test_returns_exact_symbol_match_name(self, mocker):
        fake_data = {
            "data": [
                {"symbol": "AAPL.LON", "name": "Apple Inc (London)"},
                {"symbol": "AAPL", "name": "Apple Inc"},
            ]
        }
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_data
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        assert fetch_company_name("AAPL", "fake_key") == "Apple Inc"

    def test_raises_on_empty_matches(self, mocker):
        mock_resp = mocker.Mock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"data": []}
        mock_resp.raise_for_status = mocker.Mock()
        mocker.patch("app.twelve_data_client.requests.get", return_value=mock_resp)

        with pytest.raises(MarketDataSymbolNotFound, match="No matching company"):
            fetch_company_name("ZZZZZZ", "fake_key")
