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
        # First monotonic() reserves the slot under the lock; the second
        # computes how long to sleep after the lock is released.
        mocker.patch("app.market_data.provider.time.monotonic", side_effect=[100.2, 100.2])
        sleep = mocker.patch("app.market_data.provider.time.sleep")

        TwelveDataProvider._wait_for_slot()

        sleep.assert_called_once()
        # Next slot is reserved at 100.0 + 0.5 = 100.5, so sleep ~0.3s.
        assert sleep.call_args.args[0] == pytest.approx(0.3)
        assert TwelveDataProvider._last_call_at == pytest.approx(100.5)
        TwelveDataProvider._last_call_at = None

    def test_interval_slot_reserved_without_holding_lock_during_sleep(self, mocker):
        # Two concurrent callers must reserve distinct, correctly-spaced slots
        # without holding the lock while sleeping, so their waits can overlap.
        TwelveDataProvider._last_call_at = None
        mocker.patch(
            "app.market_data.provider.get_twelve_data_min_interval_seconds",
            return_value=8.0,
        )
        slept = []

        # The lock must be free while sleeping; assert it is acquirable then.
        def fake_sleep(secs):
            assert TwelveDataProvider._lock.acquire(blocking=False)
            TwelveDataProvider._lock.release()
            slept.append(secs)

        mocker.patch("app.market_data.provider.time.sleep", side_effect=fake_sleep)
        # Caller A reserves at t=0 (no prior call) -> no sleep.
        # Caller B reserves at t=0 -> spaced to t=8 -> sleeps ~8s.
        mocker.patch(
            "app.market_data.provider.time.monotonic",
            side_effect=[0.0, 0.0, 0.0, 0.0],
        )

        TwelveDataProvider._wait_for_slot()
        TwelveDataProvider._wait_for_slot()

        assert slept == [pytest.approx(8.0)]
        assert TwelveDataProvider._last_call_at == pytest.approx(8.0)
        TwelveDataProvider._last_call_at = None

    def test_credit_budget_gate_allows_calls_under_budget(self, mocker):
        TwelveDataProvider._call_window.clear()
        mocker.patch(
            "app.market_data.provider.get_twelve_data_credits_per_minute",
            return_value=50,
        )
        mocker.patch("app.market_data.provider.time.monotonic", return_value=1000.0)
        sleep = mocker.patch("app.market_data.provider.time.sleep")

        for _ in range(5):
            TwelveDataProvider._wait_for_slot()

        sleep.assert_not_called()
        assert len(TwelveDataProvider._call_window) == 5
        TwelveDataProvider._call_window.clear()

    def test_credit_budget_gate_sleeps_when_budget_exhausted(self, mocker):
        TwelveDataProvider._call_window.clear()
        # Two prior calls at t=1000 fill a budget of 2.
        TwelveDataProvider._call_window.extend([1000.0, 1000.0])
        mocker.patch(
            "app.market_data.provider.get_twelve_data_credits_per_minute",
            return_value=2,
        )
        # now=1010: oldest call leaves the window at 1000+60=1060, so the
        # reserved slot is 1060 and we sleep ~50s after releasing the lock.
        mocker.patch(
            "app.market_data.provider.time.monotonic",
            side_effect=[1010.0, 1010.0],
        )
        sleep = mocker.patch("app.market_data.provider.time.sleep")

        TwelveDataProvider._wait_for_slot()

        sleep.assert_called_once()
        assert sleep.call_args.args[0] == pytest.approx(50.0)
        TwelveDataProvider._call_window.clear()

    def test_credit_budget_gate_prunes_expired_calls(self, mocker):
        TwelveDataProvider._call_window.clear()
        # An old call outside the 60s window should be pruned, not counted.
        TwelveDataProvider._call_window.append(900.0)
        mocker.patch(
            "app.market_data.provider.get_twelve_data_credits_per_minute",
            return_value=1,
        )
        mocker.patch("app.market_data.provider.time.monotonic", return_value=1000.0)
        sleep = mocker.patch("app.market_data.provider.time.sleep")

        TwelveDataProvider._wait_for_slot()

        sleep.assert_not_called()
        assert list(TwelveDataProvider._call_window) == [1000.0]
        TwelveDataProvider._call_window.clear()

    def test_supports_parallel_fetch_flags(self):
        assert TwelveDataProvider.supports_parallel_fetch is True
        assert AlphaVantageProvider.supports_parallel_fetch is False


class TestCreateMarketDataProvider:
    def test_returns_alpha_vantage_provider_by_default(self, mocker):
        mocker.patch("app.main.get_market_data_provider", return_value="alphavantage")

        provider = _create_market_data_provider()

        assert isinstance(provider, AlphaVantageProvider)

    def test_returns_twelve_data_provider_when_configured(self, mocker):
        mocker.patch("app.main.get_market_data_provider", return_value="twelvedata")

        provider = _create_market_data_provider()

        assert isinstance(provider, TwelveDataProvider)
