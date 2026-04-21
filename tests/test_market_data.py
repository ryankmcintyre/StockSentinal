"""Tests for the market data service staleness helpers."""

from datetime import date, datetime

import pytest

from app.market_data import (
    _last_completed_trading_day,
    _last_completed_trading_week_end,
    daily_data_is_stale,
    refresh_all_positions,
    refresh_position,
    weekly_data_is_stale,
)


# ---------------------------------------------------------------------------
# Lightweight stub for Position (only cache-related fields are needed)
# ---------------------------------------------------------------------------


class FakePosition:
    def __init__(
        self,
        ticker="AAPL",
        investment_type="long-term",
        daily_market_date=None,
        weekly_market_date=None,
        refresh_error=None,
        daily_close=None,
        daily_sma_21=None,
        daily_retrieved_at=None,
        weekly_close=None,
        weekly_sma_20=None,
        weekly_retrieved_at=None,
    ):
        self.ticker = ticker
        self.investment_type = investment_type
        self.daily_market_date = daily_market_date
        self.weekly_market_date = weekly_market_date
        self.refresh_error = refresh_error
        self.daily_close = daily_close
        self.daily_sma_21 = daily_sma_21
        self.daily_retrieved_at = daily_retrieved_at
        self.weekly_close = weekly_close
        self.weekly_sma_20 = weekly_sma_20
        self.weekly_retrieved_at = weekly_retrieved_at


# ---------------------------------------------------------------------------
# _last_completed_trading_day
# ---------------------------------------------------------------------------


class TestLastCompletedTradingDay:
    def test_monday_returns_friday(self):
        # Monday 2026-04-20 → previous Friday 2026-04-17
        result = _last_completed_trading_day(date(2026, 4, 20))
        assert result == date(2026, 4, 17)

    def test_tuesday_returns_monday(self):
        result = _last_completed_trading_day(date(2026, 4, 21))
        assert result == date(2026, 4, 20)

    def test_saturday_returns_friday(self):
        result = _last_completed_trading_day(date(2026, 4, 18))
        assert result == date(2026, 4, 17)

    def test_sunday_returns_friday(self):
        result = _last_completed_trading_day(date(2026, 4, 19))
        assert result == date(2026, 4, 17)

    def test_wednesday_returns_tuesday(self):
        result = _last_completed_trading_day(date(2026, 4, 22))
        assert result == date(2026, 4, 21)


# ---------------------------------------------------------------------------
# _last_completed_trading_week_end
# ---------------------------------------------------------------------------


class TestLastCompletedTradingWeekEnd:
    def test_friday_returns_previous_friday(self):
        # If today is Friday, completed week ended *last* Friday
        result = _last_completed_trading_week_end(date(2026, 4, 17))  # Friday
        assert result == date(2026, 4, 10)

    def test_saturday_returns_same_week_friday(self):
        result = _last_completed_trading_week_end(date(2026, 4, 18))  # Saturday
        assert result == date(2026, 4, 17)

    def test_sunday_returns_same_week_friday(self):
        result = _last_completed_trading_week_end(date(2026, 4, 19))  # Sunday
        assert result == date(2026, 4, 17)

    def test_monday_returns_previous_friday(self):
        result = _last_completed_trading_week_end(date(2026, 4, 20))  # Monday
        assert result == date(2026, 4, 17)

    def test_thursday_returns_previous_friday(self):
        result = _last_completed_trading_week_end(date(2026, 4, 16))  # Thursday
        assert result == date(2026, 4, 10)


# ---------------------------------------------------------------------------
# daily_data_is_stale
# ---------------------------------------------------------------------------


class TestDailyDataIsStale:
    def test_none_market_date_is_stale(self):
        pos = FakePosition(daily_market_date=None)
        assert daily_data_is_stale(pos, today=date(2026, 4, 21)) is True

    def test_current_data_is_not_stale(self):
        # Today is Tuesday 2026-04-21; completed trading day is Monday 2026-04-20
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
        # Today is Monday 2026-04-20; completed week ended Friday 2026-04-17
        pos = FakePosition(weekly_market_date=date(2026, 4, 17))
        assert weekly_data_is_stale(pos, today=date(2026, 4, 20)) is False

    def test_old_data_is_stale(self):
        pos = FakePosition(weekly_market_date=date(2026, 4, 10))
        assert weekly_data_is_stale(pos, today=date(2026, 4, 20)) is True


# ---------------------------------------------------------------------------
# refresh_position
# ---------------------------------------------------------------------------


class TestRefreshPosition:
    def test_refreshes_daily_when_daily_is_stale(self, mocker):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=False)

        refresh_position(position, db, force=False)

        daily_refresh.assert_called_once_with(position, "key")
        weekly_refresh.assert_not_called()
        assert position.refresh_error is None
        db.commit.assert_called_once()

    def test_refreshes_weekly_when_weekly_is_stale_long_term(self, mocker):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=True)

        refresh_position(position, db, force=False)

        daily_refresh.assert_not_called()
        weekly_refresh.assert_called_once_with(position, "key")
        assert position.refresh_error is None
        db.commit.assert_called_once()

    def test_skips_weekly_for_short_term_positions(self, mocker):
        position = FakePosition(investment_type="short-term")
        db = mocker.Mock()

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=True)

        refresh_position(position, db, force=False)

        daily_refresh.assert_called_once_with(position, "key")
        weekly_refresh.assert_not_called()
        assert position.refresh_error is None

    def test_persists_combined_daily_and_weekly_errors(self, mocker):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=True)
        mocker.patch("app.market_data._refresh_daily", side_effect=RuntimeError("daily boom"))
        mocker.patch("app.market_data._refresh_weekly", side_effect=RuntimeError("weekly boom"))

        refresh_position(position, db, force=False)

        assert "Daily refresh failed: daily boom" in position.refresh_error
        assert "Weekly refresh failed: weekly boom" in position.refresh_error
        db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# refresh_all_positions
# ---------------------------------------------------------------------------


class TestRefreshAllPositions:
    def test_refreshes_only_stale_positions(self, mocker):
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(ticker="MSFT", investment_type="long-term")
        pos3 = FakePosition(ticker="GOOG", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2, pos3]

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch(
            "app.market_data.daily_data_is_stale", side_effect=[True, False, False]
        )
        mocker.patch(
            "app.market_data.weekly_data_is_stale", side_effect=[False, True, False]
        )
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")

        refreshed = refresh_all_positions(db, force=False)

        assert refreshed == 2
        # pos1 needs daily, pos2 needs weekly
        daily_refresh.assert_called_once()
        weekly_refresh.assert_called_once()

    def test_force_refreshes_every_position(self, mocker):
        positions = [
            FakePosition(ticker="AAPL", investment_type="long-term"),
            FakePosition(ticker="MSFT", investment_type="short-term"),
        ]
        db = mocker.Mock()
        db.query.return_value.all.return_value = positions

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")

        refreshed = refresh_all_positions(db, force=True)

        assert refreshed == 2
        # Each unique ticker gets daily refresh; only long-term gets weekly
        assert daily_refresh.call_count == 2
        assert weekly_refresh.call_count == 1

    def test_deduplicates_api_calls_for_same_ticker(self, mocker):
        """Two positions with the same ticker should trigger only one set of API calls."""
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(ticker="AAPL", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2]

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=True)
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")

        refreshed = refresh_all_positions(db, force=False)

        assert refreshed == 2
        # Only 1 API call each despite 2 positions
        daily_refresh.assert_called_once()
        weekly_refresh.assert_called_once()

    def test_copies_daily_cache_to_duplicate_tickers(self, mocker):
        """Cached daily data should be copied from representative to duplicates."""
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(ticker="AAPL", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2]

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=False)

        def set_daily(position, api_key):
            position.daily_close = 150.0
            position.daily_sma_21 = 148.0
            position.daily_market_date = date(2026, 4, 20)
            position.daily_retrieved_at = datetime(2026, 4, 21, 10, 0)

        mocker.patch("app.market_data._refresh_daily", side_effect=set_daily)

        refresh_all_positions(db, force=False)

        # Both positions should have the same cached daily data
        assert pos2.daily_close == 150.0
        assert pos2.daily_sma_21 == 148.0

    def test_mixed_types_same_ticker_fetches_weekly_for_long_term(self, mocker):
        """When same ticker has both short-term and long-term positions,
        weekly data should be fetched once (for the long-term one) and
        not copied to the short-term position."""
        pos_short = FakePosition(ticker="AAPL", investment_type="short-term")
        pos_long = FakePosition(ticker="AAPL", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos_short, pos_long]

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=True)
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")

        refreshed = refresh_all_positions(db, force=False)

        assert refreshed == 2
        daily_refresh.assert_called_once()
        weekly_refresh.assert_called_once()
        # Weekly refresh should use the long-term position as representative
        weekly_refresh.assert_called_with(pos_long, "key")

    def test_short_term_positions_not_counted_for_weekly(self, mocker):
        """Short-term positions with missing weekly data should not trigger weekly refresh."""
        pos = FakePosition(ticker="AAPL", investment_type="short-term", weekly_market_date=None)
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos]

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=True)
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")

        refresh_all_positions(db, force=False)

        daily_refresh.assert_called_once()
        weekly_refresh.assert_not_called()

    def test_duplicate_error_not_propagated(self, mocker):
        """refresh_error from the representative should not be copied to duplicates."""
        pos1 = FakePosition(ticker="AAPL", investment_type="long-term")
        pos2 = FakePosition(ticker="AAPL", investment_type="long-term")
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2]

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=False)
        mocker.patch("app.market_data._refresh_daily", side_effect=RuntimeError("boom"))

        refresh_all_positions(db, force=False)

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

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=False)
        mocker.patch("app.market_data._refresh_daily", side_effect=RuntimeError("boom"))

        refresh_all_positions(db, force=False)

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

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=False)

        def set_daily(position, api_key):
            position.daily_close = 150.0
            position.daily_sma_21 = 148.0
            position.daily_market_date = date(2026, 4, 20)
            position.daily_retrieved_at = datetime(2026, 4, 21, 10, 0)

        mocker.patch("app.market_data._refresh_daily", side_effect=set_daily)

        refresh_all_positions(db, force=False)

        assert pos2.daily_close == 150.0
        assert pos2.refresh_error is None
