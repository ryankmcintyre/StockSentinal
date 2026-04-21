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
        daily_market_date=None,
        weekly_market_date=None,
        refresh_error=None,
    ):
        self.ticker = ticker
        self.daily_market_date = daily_market_date
        self.weekly_market_date = weekly_market_date
        self.refresh_error = refresh_error


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
        position = FakePosition()
        db = mocker.Mock()

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")
        sleep = mocker.patch("app.market_data.time.sleep")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=False)

        refresh_position(position, db, force=False)

        daily_refresh.assert_called_once_with(position, "key")
        weekly_refresh.assert_not_called()
        sleep.assert_not_called()
        assert position.refresh_error is None
        db.commit.assert_called_once()

    def test_refreshes_weekly_when_weekly_is_stale(self, mocker):
        position = FakePosition()
        db = mocker.Mock()

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        daily_refresh = mocker.patch("app.market_data._refresh_daily")
        weekly_refresh = mocker.patch("app.market_data._refresh_weekly")
        sleep = mocker.patch("app.market_data.time.sleep")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=False)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=True)

        refresh_position(position, db, force=False)

        daily_refresh.assert_not_called()
        weekly_refresh.assert_called_once_with(position, "key")
        sleep.assert_called_once_with(12)
        assert position.refresh_error is None
        db.commit.assert_called_once()

    def test_persists_combined_daily_and_weekly_errors(self, mocker):
        position = FakePosition()
        db = mocker.Mock()

        mocker.patch("app.market_data.require_alpha_vantage_api_key", return_value="key")
        mocker.patch("app.market_data.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.weekly_data_is_stale", return_value=True)
        mocker.patch("app.market_data.time.sleep")
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
    def test_refreshes_positions_with_stale_data(self, mocker):
        pos1 = FakePosition()
        pos2 = FakePosition()
        pos3 = FakePosition()
        db = mocker.Mock()
        db.query.return_value.all.return_value = [pos1, pos2, pos3]

        daily_stale = mocker.patch(
            "app.market_data.daily_data_is_stale", side_effect=[True, False, False]
        )
        weekly_stale = mocker.patch(
            "app.market_data.weekly_data_is_stale", side_effect=[False, True, False]
        )
        refresh = mocker.patch("app.market_data.refresh_position")

        refreshed = refresh_all_positions(db, force=False)

        assert refreshed == 2
        assert refresh.call_count == 2
        refresh.assert_any_call(pos1, db, force=False)
        refresh.assert_any_call(pos2, db, force=False)
        daily_stale.assert_called()
        weekly_stale.assert_called()

    def test_force_refreshes_every_position(self, mocker):
        positions = [FakePosition(), FakePosition()]
        db = mocker.Mock()
        db.query.return_value.all.return_value = positions

        daily_stale = mocker.patch("app.market_data.daily_data_is_stale")
        weekly_stale = mocker.patch("app.market_data.weekly_data_is_stale")
        refresh = mocker.patch("app.market_data.refresh_position")

        refreshed = refresh_all_positions(db, force=True)

        assert refreshed == 2
        assert refresh.call_count == 2
        refresh.assert_any_call(positions[0], db, force=True)
        refresh.assert_any_call(positions[1], db, force=True)
        daily_stale.assert_not_called()
        weekly_stale.assert_not_called()
