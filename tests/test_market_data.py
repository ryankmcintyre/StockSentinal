"""Tests for the market data service layer."""

from datetime import date, datetime
from unittest.mock import Mock

import pytest

from app.market_data.staleness import (
    daily_bar_cache_is_stale,
    daily_data_is_stale,
    last_completed_trading_day,
    last_completed_trading_week_end,
    weekly_bar_cache_is_stale,
    weekly_data_is_stale,
)
from app.market_data.service import MarketDataService


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
        sector_benchmark_ticker=None,
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
        self.sector_benchmark_ticker = sector_benchmark_ticker


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


# ---------------------------------------------------------------------------
# refresh_position (service-level tests with mock provider)
# ---------------------------------------------------------------------------


class TestRefreshPosition:
    @pytest.fixture(autouse=True)
    def _setup_service(self, mocker):
        """Create a service with a mock provider and mock rule_config."""
        self.mock_provider = Mock()
        self.service = MarketDataService(self.mock_provider)
        # Mock rule_config to avoid DB dependency
        mocker.patch("app.rule_config.get_required_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_atr_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_weekly_bar_lookback", return_value=0)
        mocker.patch("app.rule_config.get_required_daily_bar_lookback", return_value=0)

    def test_refreshes_daily_when_daily_is_stale(self, mocker):
        position = FakePosition(investment_type="long-term")
        db = mocker.Mock()

        mocker.patch.object(self.service, "_refresh_daily")
        mocker.patch.object(self.service, "_refresh_weekly")
        mocker.patch("app.market_data.service.daily_data_is_stale", return_value=True)
        mocker.patch("app.market_data.service.weekly_data_is_stale", return_value=False)

        self.service.refresh_position(position, db, force=False)

        self.service._refresh_daily.assert_called_once_with(position)
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
        self.service._refresh_weekly.assert_called_once_with(position)
        assert position.refresh_error is None
        db.commit.assert_called_once()

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


# ---------------------------------------------------------------------------
# refresh_all_positions (service-level tests)
# ---------------------------------------------------------------------------


class TestRefreshAllPositions:
    @pytest.fixture(autouse=True)
    def _setup_service(self, mocker):
        """Create a service with mock provider and mock rule_config."""
        self.mock_provider = Mock()
        self.service = MarketDataService(self.mock_provider)
        mocker.patch("app.rule_config.get_required_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_atr_indicators", return_value=set())
        mocker.patch("app.rule_config.get_required_weekly_bar_lookback", return_value=0)
        mocker.patch("app.rule_config.get_required_daily_bar_lookback", return_value=0)

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

        def set_daily(position):
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
        weekly_refresh.assert_called_with(pos_long)

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

        def set_daily(position):
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

        errors = service.refresh_weekly_bar_cache(db, {"IBM"}, lookback_weeks=2, force=True)
        assert errors == []

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
            DailyBar(date=date(2025, 6, 13), close=105.0),
            DailyBar(date=date(2025, 6, 12), close=100.0),
            DailyBar(date=date(2025, 6, 11), close=98.0),
            DailyBar(date=date(2025, 6, 10), close=95.0),
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

        errors = service.refresh_daily_bar_cache(db, {"IBM"}, lookback_days=2, force=True)
        assert errors == []

        rows = (
            db.query(MarketDailyBarCache)
            .filter(MarketDailyBarCache.ticker == "IBM")
            .order_by(MarketDailyBarCache.bar_date.desc())
            .all()
        )
        assert [r.bar_date for r in rows] == [
            date(2025, 6, 13), date(2025, 6, 12), date(2025, 6, 11),
        ]
        assert rows[0].close == 105.0

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
