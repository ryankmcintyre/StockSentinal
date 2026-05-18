"""Tests for market data provider implementations and selection."""

from datetime import date

import pytest

from app.alpha_vantage_client import DailyBar, WeeklyBar
from app.main import _create_market_data_provider
from app.market_data.provider import AlphaVantageProvider, TwelveDataProvider


class TestTwelveDataProvider:
    def test_fetch_daily_bars_uses_twelve_data_client(self, mocker):
        provider = TwelveDataProvider(get_api_key=lambda: "fake_key")
        mock_wait = mocker.patch.object(provider, "_wait_for_slot")
        mock_fetch = mocker.patch(
            "app.market_data.provider._td_fetch_daily_series",
            return_value=[DailyBar(date=date(2026, 4, 17), close=185.5)],
        )

        result = provider.fetch_daily_bars("AAPL")

        mock_wait.assert_called_once_with()
        mock_fetch.assert_called_once_with("AAPL", "fake_key")
        assert result == [DailyBar(date=date(2026, 4, 17), close=185.5)]

    def test_fetch_daily_bars_batch_uses_one_rate_limited_twelve_data_call(self, mocker):
        provider = TwelveDataProvider(get_api_key=lambda: "fake_key")
        mock_wait = mocker.patch.object(provider, "_wait_for_slot")
        expected = {"AAPL": [DailyBar(date=date(2026, 4, 17), close=185.5)]}
        mock_fetch = mocker.patch(
            "app.market_data.provider._td_fetch_daily_series_batch",
            return_value=expected,
        )

        result = provider.fetch_daily_bars_batch(["AAPL", "MSFT"])

        mock_wait.assert_called_once_with()
        mock_fetch.assert_called_once_with(["AAPL", "MSFT"], "fake_key")
        assert result == expected

    def test_fetch_weekly_bars_batch_uses_twelve_data_client(self, mocker):
        provider = TwelveDataProvider(get_api_key=lambda: "fake_key")
        mocker.patch.object(provider, "_wait_for_slot")
        expected = {"AAPL": [WeeklyBar(date=date(2026, 4, 17), close=185.5)]}
        mock_fetch = mocker.patch(
            "app.market_data.provider._td_fetch_weekly_series_batch",
            return_value=expected,
        )

        assert provider.fetch_weekly_bars_batch(["AAPL"]) == expected
        mock_fetch.assert_called_once_with(["AAPL"], "fake_key")

    def test_fetch_sma_passes_interval_and_time_period(self, mocker):
        provider = TwelveDataProvider(get_api_key=lambda: "fake_key")
        mocker.patch.object(provider, "_wait_for_slot")
        mock_fetch = mocker.patch(
            "app.market_data.provider._td_fetch_sma",
            return_value=[],
        )

        provider.fetch_sma("AAPL", interval="daily", time_period=21)

        mock_fetch.assert_called_once_with(
            "AAPL",
            interval="daily",
            time_period=21,
            api_key="fake_key",
        )

    def test_rate_limit_interval_uses_configured_twelve_data_value(self, mocker):
        TwelveDataProvider._last_call_at = 100.0
        mocker.patch(
            "app.market_data.provider.get_twelve_data_min_interval_seconds",
            return_value=0.5,
        )
        mocker.patch("app.market_data.provider.time.monotonic", side_effect=[100.2, 100.7])
        sleep = mocker.patch("app.market_data.provider.time.sleep")

        TwelveDataProvider._wait_for_slot()

        sleep.assert_called_once()
        assert sleep.call_args.args[0] == pytest.approx(0.3)
        TwelveDataProvider._last_call_at = None


class TestCreateMarketDataProvider:
    def test_returns_alpha_vantage_provider_by_default(self, mocker):
        mocker.patch("app.main.get_market_data_provider", return_value="alphavantage")

        provider = _create_market_data_provider()

        assert isinstance(provider, AlphaVantageProvider)

    def test_returns_twelve_data_provider_when_configured(self, mocker):
        mocker.patch("app.main.get_market_data_provider", return_value="twelvedata")

        provider = _create_market_data_provider()

        assert isinstance(provider, TwelveDataProvider)
