"""Tests for the market data service layer."""

from datetime import date, datetime, timezone
import logging
from unittest.mock import ANY, Mock

import pytest

from app.logging_utils import refresh_logging_context
from app.schemas import Verdict
from app.market_data.staleness import (
    atr_cache_is_stale,
    daily_bar_cache_is_stale,
    daily_data_is_stale,
    indicator_cache_is_stale,
    last_completed_trading_day,
    last_completed_trading_week_end,
    weekly_bar_cache_is_stale,
    weekly_data_is_stale,
)
from app.market_data.service import MarketDataService, _FetchCache


# ---------------------------------------------------------------------------
# Lightweight stub for Position (only cache-related fields are needed)
# ---------------------------------------------------------------------------


class FakePosition:
    """Lightweight position stub with breakeven defaults for rule evaluation tests."""

    def __init__(
        self,
        ticker="AAPL",
        investment_type="long-term",
        cost_basis=100.0,
        current_price=100.0,
        initial_purchase_date=date(2025, 1, 1),
        daily_market_date=None,
        weekly_market_date=None,
        refresh_error=None,
        daily_close=None,
        daily_sma_21=None,
        daily_retrieved_at=None,
        weekly_close=None,
        weekly_sma_20=None,
        weekly_retrieved_at=None,
        sector_benchmark_ticker=None,
        user_id="test-user-id",
        previous_verdict=None,
    ):
        self.ticker = ticker
        self.investment_type = investment_type
        self.cost_basis = cost_basis
        self.current_price = current_price
        self.initial_purchase_date = initial_purchase_date
        self.daily_market_date = daily_market_date
        self.weekly_market_date = weekly_market_date
        self.refresh_error = refresh_error
        self.daily_close = daily_close
        self.daily_sma_21 = daily_sma_21
        self.daily_retrieved_at = daily_retrieved_at
        self.weekly_close = weekly_close
        self.weekly_sma_20 = weekly_sma_20
        self.weekly_retrieved_at = weekly_retrieved_at
        self.sector_benchmark_ticker = sector_benchmark_ticker
        self.user_id = user_id
        self.previous_verdict = previous_verdict


# ---------------------------------------------------------------------------
# last_completed_trading_day
# ---------------------------------------------------------------------------


class TestLastCompletedTradingDay:
    def test_monday_returns_friday(self):
        result = last_completed_trading_day(date(2026, 4, 20))
        assert result == date(2026, 4, 17)

    def test_tuesday_returns_monday(self):
        result = last_completed_trading_day(date(2026, 4, 21))
        assert result == date(2026, 4, 20)

    def test_saturday_returns_friday(self):
        result = last_completed_trading_day(date(2026, 4, 18))
        assert result == date(2026, 4, 17)

    def test_sunday_returns_friday(self):
        result = last_completed_trading_day(date(2026, 4, 19))
        assert result == date(2026, 4, 17)

    def test_wednesday_returns_tuesday(self):
        result = last_completed_trading_day(date(2026, 4, 22))
        assert result == date(2026, 4, 21)

    # --- time-aware (now= path) ---

    def test_weekday_after_close_returns_today(self):
        # Wednesday at 4:30 PM ET — market closed, today counts
        now = datetime(2026, 4, 22, 16, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_day(now=now) == date(2026, 4, 22)

    def test_weekday_before_close_returns_yesterday(self):
        # Wednesday at 10:00 AM ET — market still open
        now = datetime(2026, 4, 22, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_day(now=now) == date(2026, 4, 21)

    def test_weekday_at_exactly_close_returns_today(self):
        # Wednesday exactly at 4:00 PM ET
        now = datetime(2026, 4, 22, 16, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_day(now=now) == date(2026, 4, 22)

    def test_friday_after_close_returns_friday(self):
        now = datetime(2026, 4, 17, 17, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_day(now=now) == date(2026, 4, 17)

    def test_monday_before_close_returns_friday(self):
        now = datetime(2026, 4, 20, 9, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_day(now=now) == date(2026, 4, 17)

    def test_saturday_returns_friday_regardless_of_time(self):
        now = datetime(2026, 4, 18, 20, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_day(now=now) == date(2026, 4, 17)

    def test_naive_datetime_treated_as_et(self):
        # Naive datetime at 5 PM on a Wednesday → treated as ET, market closed
        now = datetime(2026, 4, 22, 17, 0)
        assert last_completed_trading_day(now=now) == date(2026, 4, 22)


# ---------------------------------------------------------------------------
# last_completed_trading_week_end
# ---------------------------------------------------------------------------


class TestLastCompletedTradingWeekEnd:
    def test_friday_returns_previous_friday(self):
        result = last_completed_trading_week_end(date(2026, 4, 17))  # Friday
        assert result == date(2026, 4, 10)

    def test_saturday_returns_same_week_friday(self):
        result = last_completed_trading_week_end(date(2026, 4, 18))  # Saturday
        assert result == date(2026, 4, 17)

    def test_sunday_returns_same_week_friday(self):
        result = last_completed_trading_week_end(date(2026, 4, 19))  # Sunday
        assert result == date(2026, 4, 17)

    def test_monday_returns_previous_friday(self):
        result = last_completed_trading_week_end(date(2026, 4, 20))  # Monday
        assert result == date(2026, 4, 17)

    def test_thursday_returns_previous_friday(self):
        result = last_completed_trading_week_end(date(2026, 4, 16))  # Thursday
        assert result == date(2026, 4, 10)

    # --- time-aware (now= path) ---

    def test_friday_after_close_returns_this_friday(self):
        # Friday at 4:30 PM ET — this week's close is done
        now = datetime(2026, 4, 17, 16, 30, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_week_end(now=now) == date(2026, 4, 17)

    def test_friday_before_close_returns_previous_friday(self):
        # Friday at 2:00 PM ET — week not yet closed
        now = datetime(2026, 4, 17, 14, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_week_end(now=now) == date(2026, 4, 10)

    def test_friday_at_exactly_close_returns_this_friday(self):
        now = datetime(2026, 4, 17, 16, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_week_end(now=now) == date(2026, 4, 17)

    def test_thursday_after_close_returns_previous_friday(self):
        # Thursday after close — week still not over
        now = datetime(2026, 4, 16, 17, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_week_end(now=now) == date(2026, 4, 10)

    def test_saturday_returns_this_week_friday_regardless_of_time(self):
        now = datetime(2026, 4, 18, 9, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_week_end(now=now) == date(2026, 4, 17)

    def test_monday_before_close_returns_previous_friday(self):
        now = datetime(2026, 4, 20, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("America/New_York"))
        assert last_completed_trading_week_end(now=now) == date(2026, 4, 17)


# ---------------------------------------------------------------------------
# daily_data_is_stale
# ---------------------------------------------------------------------------


class TestDailyDataIsStale:
    def test_none_market_date_is_stale(self):
        pos = FakePosition(daily_market_date=None)
        assert daily_data_is_stale(pos, today=date(2026, 4, 21)) is True

    def test_current_data_is_not_stale(self):
        pos = FakePosition(daily_market_date=date(2026, 4, 20))
        assert daily_data_is_stale(pos, today=date(2026, 4, 21)) is False

    def test_old_data_is_stale(self):
        pos = FakePosition(daily_market_date=date(2026, 4, 15))
        assert daily_data_is_stale(pos, today=date(2026, 4, 21)) is True


# ---------------------------------------------------------------------------
# weekly_data_is_stale
# ---------------------------------------------------------------------------


class TestWeeklyDataIsStale:
    def test_none_market_date_is_stale(self):
        pos = FakePosition(weekly_market_date=None)
        assert weekly_data_is_stale(pos, today=date(2026, 4, 21)) is True

    def test_current_data_is_not_stale(self):
        pos = FakePosition(weekly_market_date=date(2026, 4, 17))
        assert weekly_data_is_stale(pos, today=date(2026, 4, 20)) is False

    def test_old_data_is_stale(self):
        pos = FakePosition(weekly_market_date=date(2026, 4, 10))
        assert weekly_data_is_stale(pos, today=date(2026, 4, 20)) is True

    def test_monday_dated_same_week_data_is_not_stale(self):
        pos = FakePosition(weekly_market_date=date(2026, 4, 13))
        assert weekly_data_is_stale(pos, today=date(2026, 4, 20)) is False


class TestWeeklyIndicatorAndAtrStaleness:
    def test_weekly_indicator_cache_accepts_monday_dates_in_target_week(self):
        from app.models import MarketIndicatorCache

        cache_row = MarketIndicatorCache(
            ticker="IBM",
            interval="weekly",
            time_period=20,
            sma_value=100.0,
            sma_date=date(2026, 4, 13),
            close_value=101.0,
            close_date=date(2026, 4, 13),
        )

        assert indicator_cache_is_stale(cache_row, "weekly", today=date(2026, 4, 20)) is False

    def test_weekly_atr_cache_accepts_monday_dates_in_target_week(self):
        from app.models import MarketAtrCache

        cache_row = MarketAtrCache(
            ticker="IBM",
            interval="weekly",
            time_period=14,
            atr_value=3.5,
            atr_date=date(2026, 4, 13),
        )

        assert atr_cache_is_stale(cache_row, "weekly", today=date(2026, 4, 20)) is False


# ---------------------------------------------------------------------------
# refresh_position (service-level tests with mock provider)
# ---------------------------------------------------------------------------


class TestRefreshPosition:
    @pytest.fixture(autouse=True)
    def _setup_service(self, mocker):
        """Create a service with a mock provider and mock rule_config."""
        self.mock_provider = Mock()
        self.service = MarketDataService(self.mock_provider)
        mocker.patch.object(MarketDataService, "load_indicator_cache_for_tickers", return_value={})
        mocker.patch.object(MarketDataService, "load_atr_cache_for_tickers", return_value={})
        mocker.patch.object(
            MarketDataService, "load_weekly_bar_cache_for_tickers", return_value={}
        )
        mocker.patch.object(
            MarketDataService, "load_daily_bar_cache_for_tickers", return_value={}
        )
        # Mock rule_config to avoid DB dependency
        mocker.patch("app.rule_config.ensure_strategy_rule_defaults")
        mocker.patch("app.rule_config.get_required_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_atr_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_weekly_bar_lookback", return_value=0)
        mocker.patch("app.rule_config.get_required_daily_bar_lookback", return_value=0)
        mocker.patch(
            "app.rule_config.get_enabled_rule_selections_by_investment_type",
            return_value={},
        )

    def test_refreshes_daily_when_daily_is_stale(self, mocker):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)

        self.service.refresh_position(position, db, force=False)

        self.service._refresh_daily.assert_called_once()
        call_args = self.service._refresh_daily.call_args
        assert call_args[0][0] is position
        assert call_args[1].get("fetch_cache") is not None
        self.service._refresh_weekly.assert_not_called()
        assert position.refresh_error is None
        db.commit.assert_called_once()

    def test_refreshes_weekly_when_weekly_is_stale_long_term(self, mocker):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=True)

        self.service.refresh_position(position, db, force=False)

        self.service._refresh_daily.assert_not_called()
        self.service._refresh_weekly.assert_called_once()
        call_args = self.service._refresh_weekly.call_args
        assert call_args[0][0] is position
        assert call_args[1].get("fetch_cache") is not None
        assert position.refresh_error is None
        db.commit.assert_called_once()

    def test_skips_weekly_refresh_for_short_term_even_when_weekly_is_stale(
        self, mocker
    ):
        position = FakePosition(investment_type="short-term")
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=True)

        self.service.refresh_position(position, db, force=False)

        self.service._refresh_daily.assert_not_called()
        self.service._refresh_weekly.assert_not_called()
        assert position.refresh_error is None
        db.commit.assert_called_once()

    def test_rule_requirements_are_scoped_to_position_investment_type(self, mocker):
        position = FakePosition(investment_type="short-term")
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        ensure_defaults = mocker.patch("app.rule_config.ensure_strategy_rule_defaults")
        get_required = mocker.patch("app.rule_config.get_required_indicators", return_value=set())
        get_required_atr = mocker.patch("app.rule_config.get_required_atr_indicators", return_value=set())
        get_weekly_lookback = mocker.patch(
            "app.rule_config.get_required_weekly_bar_lookback", return_value=0
        )
        get_daily_lookback = mocker.patch(
            "app.rule_config.get_required_daily_bar_lookback", return_value=0
        )

        self.service.refresh_position(position, db, force=False)

        ensure_defaults.assert_called_once_with(ANY, user_id=position.user_id)
        get_required.assert_called_once_with(ANY, "short-term", _skip_defaults=True)
        get_required_atr.assert_called_once_with(ANY, "short-term", _skip_defaults=True)
        get_weekly_lookback.assert_called_once_with(ANY, "short-term", _skip_defaults=True)
        get_daily_lookback.assert_called_once_with(ANY, "short-term", _skip_defaults=True)

    def test_persists_combined_daily_and_weekly_errors(self, mocker):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch.object(
            self.service, "_refresh_daily", side_effect=RuntimeError("daily boom")
        )
        mocker.patch.object(
            self.service, "_refresh_weekly", side_effect=RuntimeError("weekly boom")
        )
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=True)

        self.service.refresh_position(position, db, force=False)

        assert "Daily refresh failed: daily boom" in position.refresh_error
        assert "Weekly refresh failed: weekly boom" in position.refresh_error
        db.commit.assert_called_once()

    def test_stores_previous_verdict_when_refresh_changes_status(self, mocker):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        calculate_verdicts = mocker.patch.object(
            self.service,
            "_calculate_verdicts",
            side_effect=[
                {id(position): Verdict.hold.value},
                {id(position): Verdict.trim.value},
            ],
        )

        self.service.refresh_position(position, db, force=False)

        assert position.previous_verdict == Verdict.hold.value
        assert calculate_verdicts.call_count == 2

    def test_clears_previous_verdict_when_refresh_keeps_same_status(self, mocker):
        position = FakePosition(
            investment_type="long-term",
            previous_verdict=Verdict.sell.value,
        )
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch.object(
            self.service,
            "_calculate_verdicts",
            side_effect=[
                {id(position): Verdict.hold.value},
                {id(position): Verdict.hold.value},
            ],
        )

        self.service.refresh_position(position, db, force=False)

        assert position.previous_verdict is None

    def test_stores_previous_verdict_when_rule_cache_refresh_changes_status(
        self, mocker
    ):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch(
            "app.rule_config.get_required_indicators",
            return_value={("daily", 21)},
        )
        refresh_indicator_cache = mocker.patch.object(
            self.service, "refresh_indicator_cache", return_value=[]
        )
        calculate_verdicts = mocker.patch.object(
            self.service,
            "_calculate_verdicts",
            side_effect=[
                {id(position): Verdict.hold.value},
                {id(position): Verdict.trim.value},
            ],
        )

        self.service.refresh_position(position, db, force=False)

        assert position.previous_verdict == Verdict.hold.value
        refresh_indicator_cache.assert_called_once()
        assert calculate_verdicts.call_count == 2

    def test_refresh_position_logs_phase_breakdown(self, mocker, caplog):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        caplog.set_level(logging.INFO, logger="app.market_data.service")

        with refresh_logging_context("refresh-test"):
            self.service.refresh_position(position, db, force=False)

        matching_records = [
            record for record in caplog.records
            if record.name == "app.market_data.service"
            and "refresh_position completed for AAPL" in record.getMessage()
        ]
        assert matching_records
        message = matching_records[-1].getMessage()
        assert "phases: rule_config=" in message
        assert "prewarm=" in message
        assert "commit=" in message
        assert matching_records[-1].refresh_id == "refresh-test"


# ---------------------------------------------------------------------------
# Local SMA computation tests (_refresh_daily / _refresh_weekly with cache)
# ---------------------------------------------------------------------------


class TestLocalSmaComputation:
    """Verify that _refresh_daily and _refresh_weekly compute SMA locally
    from fetched bars and do NOT call provider.fetch_sma."""

    @pytest.fixture(autouse=True)
    def _setup_service(self):
        self.mock_provider = Mock()
        self.service = MarketDataService(self.mock_provider)

    def test_refresh_daily_computes_sma_locally(self):
        from app.alpha_vantage_client import DailyBar as DB
        from app.market_data.service import _FetchCache

        # 25 daily bars with close prices 100..124
        bars = [DB(date=date(2026, 4, d + 1), close=100.0 + d) for d in range(25)]
        bars.sort(key=lambda b: b.date, reverse=True)  # most recent first

        self.mock_provider.fetch_daily_bars.return_value = bars

        cache = _FetchCache(self.mock_provider)
        position = FakePosition(investment_type="short-term")

        self.service._refresh_daily(position, fetch_cache=cache)

        # fetch_sma should NOT be called
        self.mock_provider.fetch_sma.assert_not_called()
        # SMA-21 should be computed from the 21 most recent closes
        expected_sma = sum(b.close for b in bars[:21]) / 21
        assert position.daily_sma_21 == pytest.approx(expected_sma)
        assert position.daily_close == bars[0].close
        assert position.daily_retrieved_at.tzinfo == timezone.utc

    def test_refresh_weekly_computes_sma_locally(self):
        from datetime import timedelta

        from app.alpha_vantage_client import WeeklyBar as WB
        from app.market_data.service import _FetchCache

        # 25 weekly bars ending before today
        base = date(2025, 7, 4)  # a Friday
        bars = [
            WB(date=base + timedelta(weeks=i), close=200.0 + i)
            for i in range(25)
        ]
        bars.sort(key=lambda b: b.date, reverse=True)

        self.mock_provider.fetch_weekly_bars.return_value = bars

        cache = _FetchCache(self.mock_provider)
        position = FakePosition(investment_type="long-term")

        self.service._refresh_weekly(position, fetch_cache=cache)

        # fetch_sma should NOT be called
        self.mock_provider.fetch_sma.assert_not_called()
        # SMA-20 should be computed from completed bars
        assert position.weekly_sma_20 is not None
        assert position.weekly_close is not None
        assert position.weekly_retrieved_at.tzinfo == timezone.utc

    def test_refresh_daily_fetches_bars_only_once_with_cache(self):
        from app.alpha_vantage_client import DailyBar as DB
        from app.market_data.service import _FetchCache

        bars = [DB(date=date(2026, 4, d + 1), close=100.0 + d) for d in range(25)]
        bars.sort(key=lambda b: b.date, reverse=True)
        self.mock_provider.fetch_daily_bars.return_value = bars

        cache = _FetchCache(self.mock_provider)
        pos1 = FakePosition(investment_type="short-term")
        pos2 = FakePosition(investment_type="short-term")

        self.service._refresh_daily(pos1, fetch_cache=cache)
        self.service._refresh_daily(pos2, fetch_cache=cache)

        # Only one API call despite two refresh calls
        assert self.mock_provider.fetch_daily_bars.call_count == 1


# ---------------------------------------------------------------------------
# refresh_all_positions (service-level tests)
# ---------------------------------------------------------------------------


class TestRefreshAllPositions:
    @pytest.fixture(autouse=True)
    def _setup_service(self, mocker):
        """Create a service with mock provider and mock rule_config."""
        self.mock_provider = Mock()
        self.service = MarketDataService(self.mock_provider)
        mocker.patch.object(MarketDataService, "load_indicator_cache_for_tickers", return_value={})
        mocker.patch.object(MarketDataService, "load_atr_cache_for_tickers", return_value={})
        mocker.patch.object(
            MarketDataService, "load_weekly_bar_cache_for_tickers", return_value={}
        )
        mocker.patch.object(
            MarketDataService, "load_daily_bar_cache_for_tickers", return_value={}
        )
        mocker.patch("app.rule_config.ensure_strategy_rule_defaults")
        mocker.patch("app.rule_config.get_required_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_atr_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_weekly_bar_lookback", return_value=0)
        mocker.patch("app.rule_config.get_required_daily_bar_lookback", return_value=0)
        mocker.patch(
            "app.rule_config.get_enabled_rule_selections_by_investment_type",
            return_value={},
        )

    def test_refreshes_only_stale_positions(self, mocker):
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(ticker="MSFT", investment_type="long-term")
        pos3 = FakePosition(ticker="GOOG", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2, pos3]

        mocker.patch(
            "app.market_data.service.daily_data_is_stale", side_effect=[True, False, False]
        )
        mocker.patch(
            "app.market_data.service.weekly_data_is_stale", side_effect=[False, True, False]
        )
        daily_refresh = mocker.patch.object(self.service, "_refresh_daily")
        weekly_refresh = mocker.patch.object(self.service, "_refresh_weekly")

        refreshed = self.service.refresh_all_positions(db, force=False)

        assert refreshed == 2
        daily_refresh.assert_called_once()
        weekly_refresh.assert_called_once()

    def test_force_refreshes_every_position(self, mocker):
        positions = [
            FakePosition(ticker="AAPL", investment_type="long-term"),
            FakePosition(ticker="MSFT", investment_type="short-term"),
        ]
        db = mocker.Mock()
        db.query.return_value.all.return_value = positions

        daily_refresh = mocker.patch.object(self.service, "_refresh_daily")
        weekly_refresh = mocker.patch.object(self.service, "_refresh_weekly")

        refreshed = self.service.refresh_all_positions(db, force=True)

        assert refreshed == 2
        assert daily_refresh.call_count == 2
        assert weekly_refresh.call_count == 1

    def test_refresh_all_stores_previous_verdict_when_status_changes(self, mocker):
        pos = FakePosition(ticker="AAPL", investment_type="short-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos]

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(
            self.service,
            "_calculate_verdicts",
            side_effect=[
                {id(pos): Verdict.hold.value},
                {id(pos): Verdict.sell.value},
            ],
        )

        refreshed = self.service.refresh_all_positions(db, force=True)

        assert refreshed == 1
        assert pos.previous_verdict == Verdict.hold.value

    def test_refresh_all_updates_previous_verdict_after_rule_cache_refreshes(
        self, mocker
    ):
        pos = FakePosition(ticker="AAPL", investment_type="short-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos]

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch(
            "app.rule_config.get_required_indicators",
            return_value={("daily", 21)},
        )
        parent = Mock()
        parent.attach_mock(
            mocker.patch.object(
                self.service,
                "_calculate_verdicts",
                side_effect=[
                    {id(pos): Verdict.hold.value},
                    {id(pos): Verdict.sell.value},
                ],
            ),
            "calculate_verdicts",
        )
        parent.attach_mock(
            mocker.patch.object(
                self.service,
                "refresh_indicator_cache",
                return_value=[],
            ),
            "refresh_indicator_cache",
        )

        refreshed = self.service.refresh_all_positions(db, force=False)

        assert refreshed == 0
        assert pos.previous_verdict == Verdict.hold.value
        assert [call[0] for call in parent.method_calls] == [
            "calculate_verdicts",
            "refresh_indicator_cache",
            "calculate_verdicts",
        ]

    def test_clears_refresh_state_for_each_completed_position_group(
        self, mocker
    ):
        old_started = datetime(2026, 4, 21, 9, 0, 0, tzinfo=timezone.utc)
        pos = FakePosition(ticker="AAPL", investment_type="long-term")
        later_pos = FakePosition(ticker="MSFT", investment_type="long-term")
        for current in (pos, later_pos):
            current.refresh_in_progress = True
            current.refresh_started_at = old_started
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos, later_pos]
        commits = []

        def capture_commit():
            commits.append(
                (
                    pos.refresh_in_progress,
                    pos.refresh_started_at,
                    later_pos.refresh_in_progress,
                    later_pos.refresh_started_at,
                )
            )

        db.commit.side_effect = capture_commit

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch.object(self.service, "_refresh_daily")

        self.service.refresh_all_positions(db, force=False)

        assert commits[0][0] is False
        assert commits[0][1] is None
        assert commits[0][2] is True
        assert commits[0][3] > old_started
        assert pos.refresh_in_progress is False
        assert pos.refresh_started_at is None
        assert later_pos.refresh_in_progress is False
        assert later_pos.refresh_started_at is None

    def test_user_id_scopes_refresh_all_query(self, mocker):
        positions = [FakePosition(ticker="AAPL", investment_type="long-term")]
        db = mocker.Mock()
        db.query.return_value.filter.return_value.all.return_value = positions

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        daily_refresh = mocker.patch.object(self.service, "_refresh_daily")

        refreshed = self.service.refresh_all_positions(db, user_id="test-user-id")

        assert refreshed == 1
        db.query.return_value.filter.assert_called_once()
        daily_refresh.assert_called_once()

    def test_preloads_twelve_data_style_batches_before_refreshing_positions(self, mocker):
        from datetime import timedelta

        from app.alpha_vantage_client import DailyBar, WeeklyBar

        class BatchProvider:
            supports_batch_fetch = True

            def __init__(self):
                self.daily_batch_symbols = []
                self.weekly_batch_symbols = []
                self.single_daily_calls = []
                self.single_weekly_calls = []

            def fetch_daily_bars_batch(self, symbols):
                self.daily_batch_symbols.append(list(symbols))
                return {
                    symbol: [
                        DailyBar(
                            date=date(2026, 4, 20) - timedelta(days=offset),
                            close=100.0 + offset,
                        )
                        for offset in range(25)
                    ]
                    for symbol in symbols
                }

            def fetch_weekly_bars_batch(self, symbols):
                self.weekly_batch_symbols.append(list(symbols))
                return {
                    symbol: [
                        WeeklyBar(
                            date=date(2026, 4, 17) - timedelta(weeks=offset),
                            close=200.0 + offset,
                        )
                        for offset in range(25)
                    ]
                    for symbol in symbols
                }

            def fetch_daily_bars(self, symbol):
                self.single_daily_calls.append(symbol)
                raise AssertionError("daily batch preload should satisfy refresh")

            def fetch_weekly_bars(self, symbol):
                self.single_weekly_calls.append(symbol)
                raise AssertionError("weekly batch preload should satisfy refresh")

        provider = BatchProvider()
        service = MarketDataService(provider)
        mocker.patch.object(service, "_calculate_verdicts", return_value={})
        positions = [
            FakePosition(ticker="AAPL", investment_type="long-term"),
            FakePosition(ticker="MSFT", investment_type="short-term"),
        ]
        db = mocker.Mock()
        db.query.return_value.all.return_value = positions

        refreshed = service.refresh_all_positions(db, force=True)

        assert refreshed == 2
        assert provider.daily_batch_symbols == [["AAPL", "MSFT"]]
        assert provider.weekly_batch_symbols == [["AAPL"]]
        assert provider.single_daily_calls == []
        assert provider.single_weekly_calls == []
        assert positions[0].daily_close is not None
        assert positions[0].weekly_close is not None
        assert positions[1].daily_close is not None
        assert positions[1].weekly_close is None

    def test_preloads_batches_for_rule_cache_needs(self, mocker):
        from app.alpha_vantage_client import DailyBar, WeeklyBar

        class BatchProvider:
            supports_batch_fetch = True

            def __init__(self):
                self.daily_batch_symbols = []
                self.weekly_batch_symbols = []

            def fetch_daily_bars_batch(self, symbols):
                self.daily_batch_symbols.append(list(symbols))
                return {
                    symbol: [DailyBar(date=date(2026, 4, 20), close=100.0)]
                    for symbol in symbols
                }

            def fetch_weekly_bars_batch(self, symbols):
                self.weekly_batch_symbols.append(list(symbols))
                return {
                    symbol: [WeeklyBar(date=date(2026, 4, 17), close=200.0)]
                    for symbol in symbols
                }

            def fetch_daily_bars(self, symbol):
                raise AssertionError("rule cache should use daily batch preload")

            def fetch_weekly_bars(self, symbol):
                raise AssertionError("rule cache should use weekly batch preload")

        provider = BatchProvider()
        service = MarketDataService(provider)
        mocker.patch.object(service, "_calculate_verdicts", return_value={})
        positions = [
            FakePosition(
                ticker="AAPL",
                investment_type="long-term",
                daily_market_date=date(2026, 4, 20),
                weekly_market_date=date(2026, 4, 17),
                sector_benchmark_ticker="SPY",
            ),
            FakePosition(
                ticker="MSFT",
                investment_type="short-term",
                daily_market_date=date(2026, 4, 20),
            ),
        ]
        db = mocker.Mock()
        db.query.return_value.all.return_value = positions
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch("app.rule_config.get_required_indicators", return_value={("daily", 50), ("weekly", 20)})
        mocker.patch("app.rule_config.get_required_weekly_bar_lookback", return_value=26)
        mocker.patch("app.rule_config.get_required_daily_bar_lookback", return_value=63)
        indicator_refresh = mocker.patch.object(service, "refresh_indicator_cache", return_value=[])
        weekly_cache_refresh = mocker.patch.object(service, "refresh_weekly_bar_cache", return_value=[])
        daily_cache_refresh = mocker.patch.object(service, "refresh_daily_bar_cache", return_value=[])

        refreshed = service.refresh_all_positions(db, force=False)

        assert refreshed == 0
        assert provider.daily_batch_symbols == [["AAPL", "MSFT", "SPY"]]
        assert provider.weekly_batch_symbols == [["AAPL", "MSFT"]]
        indicator_refresh.assert_called_once()
        weekly_cache_refresh.assert_called_once()
        daily_cache_refresh.assert_called_once()

    def test_deduplicates_api_calls_for_same_ticker(self, mocker):
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(ticker="AAPL", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2]

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=True)
        daily_refresh = mocker.patch.object(self.service, "_refresh_daily")
        weekly_refresh = mocker.patch.object(self.service, "_refresh_weekly")

        refreshed = self.service.refresh_all_positions(db, force=False)

        assert refreshed == 2
        daily_refresh.assert_called_once()
        weekly_refresh.assert_called_once()

    def test_copies_daily_cache_to_duplicate_tickers(self, mocker):
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(ticker="AAPL", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2]

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)

        def set_daily(position, **kwargs):
            position.daily_close = 150.0
            position.daily_sma_21 = 148.0
            position.daily_market_date = date(2026, 4, 20)
            position.daily_retrieved_at = datetime(2026, 4, 21, 10, 0)

        mocker.patch.object(self.service, "_refresh_daily", side_effect=set_daily)

        self.service.refresh_all_positions(db, force=False)

        assert pos2.daily_close == 150.0
        assert pos2.daily_sma_21 == 148.0

    def test_mixed_types_same_ticker_fetches_weekly_for_long_term(self, mocker):
        pos_short = FakePosition(ticker="AAPL", investment_type="short-term")
        pos_long = FakePosition(ticker="AAPL", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos_short, pos_long]

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=True)
        daily_refresh = mocker.patch.object(self.service, "_refresh_daily")
        weekly_refresh = mocker.patch.object(self.service, "_refresh_weekly")

        refreshed = self.service.refresh_all_positions(db, force=False)

        assert refreshed == 2
        daily_refresh.assert_called_once()
        weekly_refresh.assert_called_once()
        weekly_args, weekly_kwargs = weekly_refresh.call_args
        assert weekly_args[0] is pos_long
        assert "fetch_cache" in weekly_kwargs

    def test_short_term_positions_not_counted_for_weekly(self, mocker):
        pos = FakePosition(ticker="AAPL", investment_type="short-term", weekly_market_date=None)
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos]

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=True)
        daily_refresh = mocker.patch.object(self.service, "_refresh_daily")
        weekly_refresh = mocker.patch.object(self.service, "_refresh_weekly")

        self.service.refresh_all_positions(db, force=False)

        daily_refresh.assert_called_once()
        weekly_refresh.assert_not_called()

    def test_duplicate_error_not_propagated(self, mocker):
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(ticker="AAPL", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2]

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch.object(
            self.service, "_refresh_daily", side_effect=RuntimeError("boom")
        )

        self.service.refresh_all_positions(db, force=False)

        assert "boom" in pos1.refresh_error
        assert pos2.refresh_error is None

    def test_duplicate_preserves_existing_error_when_daily_refresh_fails(self, mocker):
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(
            ticker="AAPL",
            investment_type="long-term",
            refresh_error="previous error",
            daily_close=99.0,
        )
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2]

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch.object(
            self.service, "_refresh_daily", side_effect=RuntimeError("boom")
        )

        self.service.refresh_all_positions(db, force=False)

        assert "boom" in pos1.refresh_error
        assert pos2.refresh_error == "previous error"
        assert pos2.daily_close == 99.0

    def test_duplicate_clears_existing_error_after_successful_copy(self, mocker):
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(
            ticker="AAPL",
            investment_type="long-term",
            refresh_error="previous error",
        )
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2]

        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)

        def set_daily(position, **kwargs):
            position.daily_close = 150.0
            position.daily_sma_21 = 148.0
            position.daily_market_date = date(2026, 4, 20)
            position.daily_retrieved_at = datetime(2026, 4, 21, 10, 0)

        mocker.patch.object(self.service, "_refresh_daily", side_effect=set_daily)

        self.service.refresh_all_positions(db, force=False)

        assert pos2.daily_close == 150.0
        assert pos2.refresh_error is None


# ---------------------------------------------------------------------------
# Weekly OHLC bar cache (issue #19)
# ---------------------------------------------------------------------------


class TestLocalAtrComputation:
    """ATR cache refresh computes ATR locally when OHLC history is available."""

    def _daily_bars(self, count=15):
        from datetime import timedelta

        from app.alpha_vantage_client import DailyBar

        return [
            DailyBar(
                date=date(2026, 4, 20) - timedelta(days=offset),
                open=100.0 - offset,
                high=102.0 - offset,
                low=99.0 - offset,
                close=100.0 - offset,
            )
            for offset in range(count)
        ]

    def _daily_bars_with_smoothed_atr(self):
        from datetime import timedelta

        from app.alpha_vantage_client import DailyBar

        start = date(2026, 4, 5)
        chronological = [
            DailyBar(
                date=start + timedelta(days=offset),
                open=100.0,
                high=102.0,
                low=98.0,
                close=100.0,
            )
            for offset in range(15)
        ]
        chronological.append(
            DailyBar(
                date=start + timedelta(days=15),
                open=110.0,
                high=118.0,
                low=100.0,
                close=110.0,
            )
        )
        return list(reversed(chronological))

    def test_refresh_atr_cache_computes_daily_atr_without_provider_atr_call(self, mocker):
        provider = Mock()
        provider.fetch_daily_bars.return_value = self._daily_bars_with_smoothed_atr()
        provider.fetch_atr.return_value = []
        provider.fetch_atr_batch.return_value = {}
        atr_repo = mocker.Mock()
        service = MarketDataService(provider, atr_repo=atr_repo)
        cache = _FetchCache(provider)
        db = mocker.Mock()

        errors = service.refresh_atr_cache(
            db,
            {"AAPL"},
            {("daily", 14)},
            force=True,
            fetch_cache=cache,
        )

        assert errors == []
        provider.fetch_atr.assert_not_called()
        provider.fetch_atr_batch.assert_not_called()
        atr_repo.upsert.assert_called_once_with(
            db,
            "AAPL",
            "daily",
            14,
            atr_value=pytest.approx(5.0),
            atr_date=date(2026, 4, 20),
        )

    def test_refresh_atr_cache_falls_back_when_ohlc_history_is_insufficient(self, mocker):
        from app.alpha_vantage_client import ATRPoint

        provider = Mock()
        provider.fetch_daily_bars.return_value = self._daily_bars(count=14)
        provider.fetch_atr.return_value = [ATRPoint(date=date(2026, 4, 20), atr=2.5)]
        atr_repo = mocker.Mock()
        service = MarketDataService(provider, atr_repo=atr_repo)
        cache = _FetchCache(provider)
        db = mocker.Mock()

        errors = service.refresh_atr_cache(
            db,
            {"AAPL"},
            {("daily", 14)},
            force=True,
            fetch_cache=cache,
        )

        assert errors == []
        provider.fetch_atr.assert_called_once_with(
            "AAPL", interval="daily", time_period=14,
        )
        atr_repo.upsert.assert_called_once_with(
            db,
            "AAPL",
            "daily",
            14,
            atr_value=2.5,
            atr_date=date(2026, 4, 20),
        )

    def test_refresh_all_uses_daily_bar_batch_for_atr_without_atr_api_calls(self, mocker):
        class BatchProvider:
            supports_batch_fetch = True

            def __init__(self, bars):
                self.bars = bars
                self.daily_batch_symbols = []
                self.atr_calls = []
                self.atr_batch_calls = []

            def fetch_daily_bars_batch(self, symbols):
                self.daily_batch_symbols.append(list(symbols))
                return {symbol: self.bars for symbol in symbols}

            def fetch_daily_bars(self, symbol):
                raise AssertionError("daily batch preload should satisfy ATR refresh")

            def fetch_atr(self, symbol, interval, time_period):
                self.atr_calls.append((symbol, interval, time_period))
                raise AssertionError("local ATR should avoid fetch_atr")

            def fetch_atr_batch(self, symbols, interval, time_period):
                self.atr_batch_calls.append((list(symbols), interval, time_period))
                raise AssertionError("local ATR should avoid fetch_atr_batch")

        provider = BatchProvider(self._daily_bars())
        atr_repo = mocker.Mock()
        atr_repo.get.return_value = None
        service = MarketDataService(provider, atr_repo=atr_repo)
        mocker.patch.object(service, "_calculate_verdicts", return_value={})
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch("app.rule_config.ensure_strategy_rule_defaults")
        mocker.patch("app.rule_config.get_required_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_atr_indicators", return_value={("daily", 14)})
        mocker.patch("app.rule_config.get_required_weekly_bar_lookback", return_value=0)
        mocker.patch("app.rule_config.get_required_daily_bar_lookback", return_value=0)
        mocker.patch("app.rule_config.get_enabled_rule_selections_by_investment_type", return_value={})
        positions = [
            FakePosition(ticker="AAPL", investment_type="short-term"),
            FakePosition(ticker="MSFT", investment_type="short-term"),
        ]
        db = mocker.Mock()
        db.query.return_value.all.return_value = positions

        refreshed = service.refresh_all_positions(db, force=False)

        assert refreshed == 0
        assert provider.daily_batch_symbols == [["AAPL", "MSFT"]]
        assert provider.atr_calls == []
        assert provider.atr_batch_calls == []
        assert atr_repo.upsert.call_count == 2

    def test_refresh_all_batches_atr_fallback_for_insufficient_history(self, mocker):
        from app.alpha_vantage_client import ATRPoint

        class BatchProvider:
            supports_batch_fetch = True

            def __init__(self, bars):
                self.bars = bars
                self.atr_calls = []
                self.atr_batch_calls = []

            def fetch_daily_bars_batch(self, symbols):
                return {symbol: self.bars for symbol in symbols}

            def fetch_daily_bars(self, symbol):
                raise AssertionError("daily batch preload should satisfy ATR refresh")

            def fetch_atr(self, symbol, interval, time_period):
                self.atr_calls.append((symbol, interval, time_period))
                raise AssertionError("ATR fallback should be batch preloaded")

            def fetch_atr_batch(self, symbols, interval, time_period):
                self.atr_batch_calls.append((list(symbols), interval, time_period))
                return {
                    symbol: [ATRPoint(date=date(2026, 4, 20), atr=2.5)]
                    for symbol in symbols
                }

        provider = BatchProvider(self._daily_bars(count=14))
        atr_repo = mocker.Mock()
        atr_repo.get.return_value = None
        service = MarketDataService(provider, atr_repo=atr_repo)
        mocker.patch.object(service, "_calculate_verdicts", return_value={})
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)
        mocker.patch("app.rule_config.ensure_strategy_rule_defaults")
        mocker.patch("app.rule_config.get_required_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_atr_indicators", return_value={("daily", 14)})
        mocker.patch("app.rule_config.get_required_weekly_bar_lookback", return_value=0)
        mocker.patch("app.rule_config.get_required_daily_bar_lookback", return_value=0)
        mocker.patch("app.rule_config.get_enabled_rule_selections_by_investment_type", return_value={})
        positions = [
            FakePosition(ticker="AAPL", investment_type="short-term"),
            FakePosition(ticker="MSFT", investment_type="short-term"),
        ]
        db = mocker.Mock()
        db.query.return_value.all.return_value = positions

        refreshed = service.refresh_all_positions(db, force=False)

        assert refreshed == 0
        assert provider.atr_batch_calls == [(["AAPL", "MSFT"], "daily", 14)]
        assert provider.atr_calls == []
        assert atr_repo.upsert.call_count == 2


class TestWeeklyBarCache:
    @pytest.fixture()
    def db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.models import Base

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            yield session
        finally:
            session.close()
            engine.dispose()

    @pytest.fixture()
    def service(self):
        mock_provider = Mock()
        return MarketDataService(mock_provider)

    def test_is_stale_when_no_rows(self):
        assert weekly_bar_cache_is_stale(None) is True

    def test_is_stale_when_latest_row_is_old(self):
        from app.models import MarketWeeklyBarCache

        latest = MarketWeeklyBarCache(
            ticker="IBM", bar_date=date(2020, 1, 3), close=100.0,
            retrieved_at=datetime.now(),
        )
        assert weekly_bar_cache_is_stale(latest, today=date(2025, 6, 15)) is True

    def test_is_fresh_when_latest_row_is_current(self):
        from app.models import MarketWeeklyBarCache

        target = last_completed_trading_week_end(date(2025, 6, 15))
        latest = MarketWeeklyBarCache(
            ticker="IBM", bar_date=target, close=100.0,
            retrieved_at=datetime.now(),
        )
        assert weekly_bar_cache_is_stale(latest, today=date(2025, 6, 15)) is False

    def test_is_fresh_when_latest_row_uses_monday_of_target_week(self):
        from app.models import MarketWeeklyBarCache

        latest = MarketWeeklyBarCache(
            ticker="IBM", bar_date=date(2025, 6, 9), close=100.0,
            retrieved_at=datetime.now(timezone.utc),
        )

        assert weekly_bar_cache_is_stale(latest, today=date(2025, 6, 15)) is False

    def test_refresh_skips_when_no_tickers(self, db, service):
        assert service.refresh_weekly_bar_cache(db, set(), lookback_weeks=10) == []

    def test_refresh_skips_when_lookback_zero(self, db, service):
        assert service.refresh_weekly_bar_cache(db, {"IBM"}, lookback_weeks=0) == []

    def test_refresh_upserts_and_trims(self, db, mocker):
        from app.alpha_vantage_client import WeeklyBar
        from app.models import MarketWeeklyBarCache

        db.add(
            MarketWeeklyBarCache(
                ticker="IBM",
                bar_date=date(2020, 1, 3),
                open=10.0, high=11.0, low=9.0, close=10.5, volume=1000.0,
                retrieved_at=datetime.now(),
            )
        )
        db.commit()

        bars = [
            WeeklyBar(date=date(2025, 6, 13), open=100.0, high=110.0, low=95.0, close=105.0, volume=2000.0),
            WeeklyBar(date=date(2025, 6, 6), open=98.0, high=105.0, low=92.0, close=100.0, volume=1800.0),
            WeeklyBar(date=date(2025, 5, 30), open=95.0, high=102.0, low=90.0, close=98.0, volume=1700.0),
        ]

        mock_provider = Mock()
        mock_provider.fetch_weekly_bars.return_value = bars
        service = MarketDataService(mock_provider)

        mocker.patch(
            "app.market_data.staleness.last_completed_trading_week_end",
            return_value=date(2025, 6, 13),
        )
        mocker.patch(
            "app.market_data.cache_repos.last_completed_trading_week_end",
            return_value=date(2025, 6, 13),
        )
        mock_datetime = mocker.patch("app.market_data.cache_repos.datetime")
        fake_now = datetime(2025, 6, 13, 20, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = fake_now

        errors = service.refresh_weekly_bar_cache(db, {"IBM"}, lookback_weeks=2, force=True)
        assert errors == []
        mock_datetime.now.assert_called_once_with(timezone.utc)

        rows = (
            db.query(MarketWeeklyBarCache)
            .filter(MarketWeeklyBarCache.ticker == "IBM")
            .order_by(MarketWeeklyBarCache.bar_date.desc())
            .all()
        )
        assert [r.bar_date for r in rows] == [date(2025, 6, 13), date(2025, 6, 6)]
        assert rows[0].open == 100.0
        assert rows[0].high == 110.0
        assert rows[0].low == 95.0
        assert rows[0].close == 105.0
        assert rows[0].volume == 2000.0

    @pytest.mark.parametrize(
        ("existing_rows", "bars", "expected_dates", "expected_close_by_date"),
        [
            pytest.param(
                [],
                [
                    {"date": date(2025, 6, 13), "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 2000.0},
                    {"date": date(2025, 6, 6), "open": 98.0, "high": 105.0, "low": 92.0, "close": 100.0, "volume": 1800.0},
                ],
                [date(2025, 6, 13), date(2025, 6, 6)],
                {date(2025, 6, 13): 105.0, date(2025, 6, 6): 100.0},
                id="insert",
            ),
            pytest.param(
                [
                    {"bar_date": date(2025, 6, 13), "close": 90.0},
                    {"bar_date": date(2025, 6, 6), "close": 80.0},
                ],
                [
                    {"date": date(2025, 6, 13), "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 2000.0},
                    {"date": date(2025, 6, 6), "open": 98.0, "high": 105.0, "low": 92.0, "close": 100.0, "volume": 1800.0},
                ],
                [date(2025, 6, 13), date(2025, 6, 6)],
                {date(2025, 6, 13): 105.0, date(2025, 6, 6): 100.0},
                id="update",
            ),
            pytest.param(
                [{"bar_date": date(2025, 6, 13), "close": 90.0}],
                [
                    {"date": date(2025, 6, 13), "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 2000.0},
                    {"date": date(2025, 6, 6), "open": 98.0, "high": 105.0, "low": 92.0, "close": 100.0, "volume": 1800.0},
                ],
                [date(2025, 6, 13), date(2025, 6, 6)],
                {date(2025, 6, 13): 105.0, date(2025, 6, 6): 100.0},
                id="mixed",
            ),
        ],
    )
    def test_repository_bulk_upsert_handles_insert_update_and_mixed_cases(
        self, db, mocker, existing_rows, bars, expected_dates, expected_close_by_date,
    ):
        from app.alpha_vantage_client import WeeklyBar
        from app.market_data.cache_repos import WeeklyBarCacheRepository
        from app.models import MarketWeeklyBarCache

        stale_timestamp = datetime(2020, 1, 1)
        for row in existing_rows:
            db.add(
                MarketWeeklyBarCache(
                    ticker="IBM",
                    bar_date=row["bar_date"],
                    close=row["close"],
                    retrieved_at=stale_timestamp,
                )
            )
        db.commit()

        mocker.patch(
            "app.market_data.cache_repos.last_completed_trading_week_end",
            return_value=date(2025, 6, 13),
        )

        WeeklyBarCacheRepository().upsert_bars(
            db,
            "IBM",
            [WeeklyBar(**bar) for bar in bars],
            lookback_weeks=2,
        )
        db.commit()

        rows = (
            db.query(MarketWeeklyBarCache)
            .filter(MarketWeeklyBarCache.ticker == "IBM")
            .order_by(MarketWeeklyBarCache.bar_date.desc())
            .all()
        )
        assert [row.bar_date for row in rows] == expected_dates
        assert {row.bar_date: row.close for row in rows} == expected_close_by_date
        assert all(row.retrieved_at is not None for row in rows)
        if existing_rows:
            assert all(row.retrieved_at > stale_timestamp for row in rows)

    def test_refresh_skips_when_cache_has_required_history_for_same_week(self, db, service, mocker):
        from app.models import MarketWeeklyBarCache

        db.add_all([
            MarketWeeklyBarCache(
                ticker="IBM",
                bar_date=date(2025, 6, 9),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1000.0,
                retrieved_at=datetime.now(timezone.utc),
            ),
            MarketWeeklyBarCache(
                ticker="IBM",
                bar_date=date(2025, 6, 2),
                open=98.0,
                high=99.0,
                low=97.0,
                close=98.5,
                volume=900.0,
                retrieved_at=datetime.now(timezone.utc),
            ),
        ])
        db.commit()

        mocker.patch(
            "app.market_data.staleness.last_completed_trading_week_end",
            return_value=date(2025, 6, 13),
        )

        errors = service.refresh_weekly_bar_cache(db, {"IBM"}, lookback_weeks=2)

        assert errors == []
        service._provider.fetch_weekly_bars.assert_not_called()

    def test_refresh_records_error_on_exception(self, db, mocker):
        mock_provider = Mock()
        mock_provider.fetch_weekly_bars.side_effect = RuntimeError("boom")
        service = MarketDataService(mock_provider)

        errors = service.refresh_weekly_bar_cache(db, {"BAD"}, lookback_weeks=4, force=True)
        assert len(errors) == 1
        assert "BAD" in errors[0]

    def test_load_returns_dict_grouped_by_ticker_most_recent_first(self, db):
        from app.models import MarketWeeklyBarCache

        for d in [date(2025, 6, 6), date(2025, 6, 13)]:
            db.add(
                MarketWeeklyBarCache(
                    ticker="A", bar_date=d, open=1.0, high=1.0, low=1.0, close=1.0,
                    volume=1.0, retrieved_at=datetime.now(),
                )
            )
        db.add(
            MarketWeeklyBarCache(
                ticker="B", bar_date=date(2025, 6, 13), open=1.0, high=1.0, low=1.0,
                close=1.0, volume=1.0, retrieved_at=datetime.now(),
            )
        )
        db.commit()

        service = MarketDataService(Mock())
        result = service.load_weekly_bar_cache_for_tickers(db, {"A", "B", "MISSING"})
        assert set(result.keys()) == {"A", "B"}
        assert [r.bar_date for r in result["A"]] == [date(2025, 6, 13), date(2025, 6, 6)]
        assert len(result["B"]) == 1

    def test_load_returns_empty_dict_for_no_tickers(self, db):
        service = MarketDataService(Mock())
        assert service.load_weekly_bar_cache_for_tickers(db, set()) == {}


class TestDailyBarCache:
    @pytest.fixture()
    def db(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from app.models import Base

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        session = Session()
        try:
            yield session
        finally:
            session.close()
            engine.dispose()

    def test_is_stale_when_no_rows(self):
        assert daily_bar_cache_is_stale(None) is True

    def test_is_stale_when_latest_row_is_old(self):
        from app.models import MarketDailyBarCache

        latest = MarketDailyBarCache(
            ticker="IBM", bar_date=date(2020, 1, 3), close=100.0,
            retrieved_at=datetime.now(),
        )
        assert daily_bar_cache_is_stale(latest, today=date(2025, 6, 15)) is True

    def test_is_fresh_when_latest_row_is_current(self):
        from app.models import MarketDailyBarCache

        target = last_completed_trading_day(date(2025, 6, 15))
        latest = MarketDailyBarCache(
            ticker="IBM", bar_date=target, close=100.0,
            retrieved_at=datetime.now(),
        )
        assert daily_bar_cache_is_stale(latest, today=date(2025, 6, 15)) is False

    def test_refresh_skips_when_no_tickers(self, db):
        service = MarketDataService(Mock())
        assert service.refresh_daily_bar_cache(db, set(), lookback_days=10) == []

    def test_refresh_skips_when_lookback_zero(self, db):
        service = MarketDataService(Mock())
        assert service.refresh_daily_bar_cache(db, {"IBM"}, lookback_days=0) == []

    def test_refresh_upserts_and_trims(self, db, mocker):
        from app.alpha_vantage_client import DailyBar
        from app.models import MarketDailyBarCache

        db.add(MarketDailyBarCache(
            ticker="IBM", bar_date=date(2020, 1, 3), close=10.0,
            retrieved_at=datetime.now(),
        ))
        db.commit()

        bars = [
            DailyBar(date=date(2025, 6, 13), open=100.0, high=110.0, low=95.0, close=105.0, volume=2000.0),
            DailyBar(date=date(2025, 6, 12), open=98.0, high=105.0, low=92.0, close=100.0, volume=1800.0),
            DailyBar(date=date(2025, 6, 11), open=95.0, high=102.0, low=90.0, close=98.0, volume=1700.0),
            DailyBar(date=date(2025, 6, 10), open=93.0, high=99.0, low=91.0, close=95.0, volume=1600.0),
        ]

        mock_provider = Mock()
        mock_provider.fetch_daily_bars.return_value = bars
        service = MarketDataService(mock_provider)

        mocker.patch(
            "app.market_data.staleness.last_completed_trading_day",
            return_value=date(2025, 6, 13),
        )
        mocker.patch(
            "app.market_data.cache_repos.last_completed_trading_day",
            return_value=date(2025, 6, 13),
        )
        mock_datetime = mocker.patch("app.market_data.cache_repos.datetime")
        fake_now = datetime(2025, 6, 13, 20, 0, tzinfo=timezone.utc)
        mock_datetime.now.return_value = fake_now

        errors = service.refresh_daily_bar_cache(db, {"IBM"}, lookback_days=2, force=True)
        assert errors == []
        mock_datetime.now.assert_called_once_with(timezone.utc)

        rows = (
            db.query(MarketDailyBarCache)
            .filter(MarketDailyBarCache.ticker == "IBM")
            .order_by(MarketDailyBarCache.bar_date.desc())
            .all()
        )
        assert [r.bar_date for r in rows] == [
            date(2025, 6, 13), date(2025, 6, 12), date(2025, 6, 11),
        ]
        assert rows[0].open == 100.0
        assert rows[0].high == 110.0
        assert rows[0].low == 95.0
        assert rows[0].close == 105.0
        assert rows[0].volume == 2000.0

    @pytest.mark.parametrize(
        ("existing_rows", "bars", "expected_dates", "expected_close_by_date"),
        [
            pytest.param(
                [],
                [
                    {"date": date(2025, 6, 13), "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 2000.0},
                    {"date": date(2025, 6, 12), "open": 98.0, "high": 105.0, "low": 92.0, "close": 100.0, "volume": 1800.0},
                ],
                [date(2025, 6, 13), date(2025, 6, 12)],
                {date(2025, 6, 13): 105.0, date(2025, 6, 12): 100.0},
                id="insert",
            ),
            pytest.param(
                [
                    {"bar_date": date(2025, 6, 13), "close": 90.0},
                    {"bar_date": date(2025, 6, 12), "close": 80.0},
                ],
                [
                    {"date": date(2025, 6, 13), "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 2000.0},
                    {"date": date(2025, 6, 12), "open": 98.0, "high": 105.0, "low": 92.0, "close": 100.0, "volume": 1800.0},
                ],
                [date(2025, 6, 13), date(2025, 6, 12)],
                {date(2025, 6, 13): 105.0, date(2025, 6, 12): 100.0},
                id="update",
            ),
            pytest.param(
                [{"bar_date": date(2025, 6, 13), "close": 90.0}],
                [
                    {"date": date(2025, 6, 13), "open": 100.0, "high": 110.0, "low": 95.0, "close": 105.0, "volume": 2000.0},
                    {"date": date(2025, 6, 12), "open": 98.0, "high": 105.0, "low": 92.0, "close": 100.0, "volume": 1800.0},
                ],
                [date(2025, 6, 13), date(2025, 6, 12)],
                {date(2025, 6, 13): 105.0, date(2025, 6, 12): 100.0},
                id="mixed",
            ),
        ],
    )
    def test_repository_bulk_upsert_handles_insert_update_and_mixed_cases(
        self, db, mocker, existing_rows, bars, expected_dates, expected_close_by_date,
    ):
        from app.alpha_vantage_client import DailyBar
        from app.market_data.cache_repos import DailyBarCacheRepository
        from app.models import MarketDailyBarCache

        stale_timestamp = datetime(2020, 1, 1)
        for row in existing_rows:
            db.add(
                MarketDailyBarCache(
                    ticker="IBM",
                    bar_date=row["bar_date"],
                    close=row["close"],
                    retrieved_at=stale_timestamp,
                )
            )
        db.commit()

        mocker.patch(
            "app.market_data.cache_repos.last_completed_trading_day",
            return_value=date(2025, 6, 13),
        )

        DailyBarCacheRepository().upsert_bars(
            db,
            "IBM",
            [DailyBar(**bar) for bar in bars],
            lookback_days=1,
        )
        db.commit()

        rows = (
            db.query(MarketDailyBarCache)
            .filter(MarketDailyBarCache.ticker == "IBM")
            .order_by(MarketDailyBarCache.bar_date.desc())
            .all()
        )
        assert [row.bar_date for row in rows] == expected_dates
        assert {row.bar_date: row.close for row in rows} == expected_close_by_date
        assert all(row.retrieved_at is not None for row in rows)
        if existing_rows:
            assert all(row.retrieved_at > stale_timestamp for row in rows)

    def test_load_returns_per_ticker_lists_most_recent_first(self, db):
        from app.models import MarketDailyBarCache

        db.add_all([
            MarketDailyBarCache(ticker="IBM", bar_date=date(2025, 1, 2), close=11.0,
                                retrieved_at=datetime.now()),
            MarketDailyBarCache(ticker="IBM", bar_date=date(2025, 1, 3), close=12.0,
                                retrieved_at=datetime.now()),
            MarketDailyBarCache(ticker="SMH", bar_date=date(2025, 1, 3), close=200.0,
                                retrieved_at=datetime.now()),
        ])
        db.commit()

        service = MarketDataService(Mock())
        result = service.load_daily_bar_cache_for_tickers(db, {"IBM", "SMH"})
        assert [r.bar_date for r in result["IBM"]] == [date(2025, 1, 3), date(2025, 1, 2)]
        assert [r.bar_date for r in result["SMH"]] == [date(2025, 1, 3)]

    def test_load_empty_tickers_returns_empty(self, db):
        service = MarketDataService(Mock())
        assert service.load_daily_bar_cache_for_tickers(db, set()) == {}


# ---------------------------------------------------------------------------
# Parallel fetch prewarm and _FetchCache thread-safety
# ---------------------------------------------------------------------------


class TestParallelPrewarm:
    def test_prewarm_fetches_daily_and_weekly_concurrently(self):
        import threading

        from app.alpha_vantage_client import DailyBar, WeeklyBar

        # A barrier of 2 only releases when both fetches are in flight at the
        # same time, proving the prewarm runs them concurrently rather than in
        # series (a sequential run would time out at the barrier).
        barrier = threading.Barrier(2, timeout=5)

        class ParallelProvider:
            supports_parallel_fetch = True

            def __init__(self):
                self.daily_calls = 0
                self.weekly_calls = 0

            def fetch_daily_bars(self, symbol):
                barrier.wait()
                self.daily_calls += 1
                return [DailyBar(date=date(2025, 1, 2), close=10.0)]

            def fetch_weekly_bars(self, symbol):
                barrier.wait()
                self.weekly_calls += 1
                return [WeeklyBar(date=date(2025, 1, 3), close=11.0)]

        provider = ParallelProvider()
        service = MarketDataService(provider)
        cache = _FetchCache(provider)

        service._prewarm_fetch_cache(
            cache, "AAPL",
            need_daily=True, need_weekly=True,
            benchmark=None, need_benchmark_daily=False,
        )

        assert provider.daily_calls == 1
        assert provider.weekly_calls == 1
        # Cache is warmed, so subsequent reads do not hit the provider again.
        assert cache.get_daily_bars("AAPL")
        assert cache.get_weekly_bars("AAPL")
        assert provider.daily_calls == 1
        assert provider.weekly_calls == 1

    def test_prewarm_is_noop_when_provider_lacks_parallel_support(self):
        from app.alpha_vantage_client import DailyBar

        class SequentialProvider:
            supports_parallel_fetch = False

            def __init__(self):
                self.daily_calls = 0

            def fetch_daily_bars(self, symbol):
                self.daily_calls += 1
                return [DailyBar(date=date(2025, 1, 2), close=10.0)]

            def fetch_weekly_bars(self, symbol):
                return []

        provider = SequentialProvider()
        service = MarketDataService(provider)
        cache = _FetchCache(provider)

        service._prewarm_fetch_cache(
            cache, "AAPL",
            need_daily=True, need_weekly=True,
            benchmark=None, need_benchmark_daily=False,
        )

        assert provider.daily_calls == 0

    def test_prewarm_includes_benchmark_daily(self):
        from app.alpha_vantage_client import DailyBar, WeeklyBar

        class ParallelProvider:
            supports_parallel_fetch = True

            def __init__(self):
                self.daily_tickers = []

            def fetch_daily_bars(self, symbol):
                self.daily_tickers.append(symbol)
                return [DailyBar(date=date(2025, 1, 2), close=10.0)]

            def fetch_weekly_bars(self, symbol):
                return [WeeklyBar(date=date(2025, 1, 3), close=11.0)]

        provider = ParallelProvider()
        service = MarketDataService(provider)
        cache = _FetchCache(provider)

        service._prewarm_fetch_cache(
            cache, "AAPL",
            need_daily=True, need_weekly=False,
            benchmark="SMH", need_benchmark_daily=True,
        )

        assert sorted(provider.daily_tickers) == ["AAPL", "SMH"]

    def test_fetch_cache_get_daily_bars_dedupes_under_concurrency(self):
        import threading

        from app.alpha_vantage_client import DailyBar

        call_count = {"n": 0}
        gate = threading.Event()

        class SlowProvider:
            supports_parallel_fetch = True

            def fetch_daily_bars(self, symbol):
                gate.wait(timeout=5)
                call_count["n"] += 1
                return [DailyBar(date=date(2025, 1, 2), close=10.0)]

        cache = _FetchCache(SlowProvider())
        results = []

        def worker():
            results.append(cache.get_daily_bars("AAPL"))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        gate.set()
        for t in threads:
            t.join(timeout=5)

        # Concurrent first-readers may each issue a fetch, but the cache must
        # converge on a single stored value that every caller observes.
        assert all(r is results[0] for r in results)

    def test_prewarm_dedupes_benchmark_equal_to_ticker(self):
        from app.alpha_vantage_client import DailyBar, WeeklyBar

        class ParallelProvider:
            supports_parallel_fetch = True

            def __init__(self):
                self.daily_tickers = []

            def fetch_daily_bars(self, symbol):
                self.daily_tickers.append(symbol)
                return [DailyBar(date=date(2025, 1, 2), close=10.0)]

            def fetch_weekly_bars(self, symbol):
                return [WeeklyBar(date=date(2025, 1, 3), close=11.0)]

        provider = ParallelProvider()
        service = MarketDataService(provider)
        cache = _FetchCache(provider)

        # Benchmark resolves to the same symbol as the position's daily fetch
        # (case-insensitive); only one daily fetch should be enqueued so we do
        # not burn a duplicate provider credit on the same symbol.
        service._prewarm_fetch_cache(
            cache, "AAPL",
            need_daily=True, need_weekly=True,
            benchmark="aapl", need_benchmark_daily=True,
        )

        assert provider.daily_tickers == ["AAPL"]

    def test_prewarm_keeps_benchmark_when_daily_not_needed(self):
        from app.alpha_vantage_client import DailyBar, WeeklyBar

        class ParallelProvider:
            supports_parallel_fetch = True

            def __init__(self):
                self.daily_tickers = []

            def fetch_daily_bars(self, symbol):
                self.daily_tickers.append(symbol)
                return [DailyBar(date=date(2025, 1, 2), close=10.0)]

            def fetch_weekly_bars(self, symbol):
                return [WeeklyBar(date=date(2025, 1, 3), close=11.0)]

        provider = ParallelProvider()
        service = MarketDataService(provider)
        cache = _FetchCache(provider)

        # When the position's own daily fetch is not needed, the benchmark
        # daily fetch must still run even if it equals the ticker symbol.
        service._prewarm_fetch_cache(
            cache, "AAPL",
            need_daily=False, need_weekly=True,
            benchmark="AAPL", need_benchmark_daily=True,
        )

        assert provider.daily_tickers == ["AAPL"]
