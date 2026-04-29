"""Cache repository classes.

Each repository encapsulates the SQLAlchemy queries for one cache table.
Repositories are stateless — they receive a ``Session`` on every call so
that callers control the transaction boundary.
"""

from datetime import date, datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import (
    MarketAtrCache,
    MarketDailyBarCache,
    MarketIndicatorCache,
    MarketWeeklyBarCache,
)

from .staleness import last_completed_trading_day, last_completed_trading_week_end


class IndicatorCacheRepository:
    """CRUD for :class:`MarketIndicatorCache` rows."""

    def get(
        self, db: Session, ticker: str, interval: str, time_period: int,
    ) -> Optional[MarketIndicatorCache]:
        return (
            db.query(MarketIndicatorCache)
            .filter(MarketIndicatorCache.ticker == ticker)
            .filter(MarketIndicatorCache.interval == interval)
            .filter(MarketIndicatorCache.time_period == time_period)
            .first()
        )

    def upsert(
        self,
        db: Session,
        ticker: str,
        interval: str,
        time_period: int,
        *,
        sma_value: Optional[float],
        sma_date: Optional[date],
        close_value: Optional[float],
        close_date: Optional[date],
    ) -> MarketIndicatorCache:
        row = self.get(db, ticker, interval, time_period)
        if row is None:
            row = MarketIndicatorCache(
                ticker=ticker, interval=interval, time_period=time_period,
            )
            db.add(row)
        row.sma_value = sma_value
        row.sma_date = sma_date
        row.close_value = close_value
        row.close_date = close_date
        row.retrieved_at = datetime.now()
        return row

    def load_for_tickers(
        self, db: Session, tickers: set[str],
    ) -> dict[str, dict[tuple[str, int], tuple[Optional[float], Optional[float]]]]:
        """Load indicator cache: {ticker: {(interval, period): (close, sma)}}"""
        if not tickers:
            return {}
        rows = (
            db.query(MarketIndicatorCache)
            .filter(MarketIndicatorCache.ticker.in_(tickers))
            .all()
        )
        result: dict[str, dict[tuple[str, int], tuple[Optional[float], Optional[float]]]] = {}
        for row in rows:
            ticker_signals = result.setdefault(row.ticker, {})
            ticker_signals[(row.interval, row.time_period)] = (row.close_value, row.sma_value)
        return result


class AtrCacheRepository:
    """CRUD for :class:`MarketAtrCache` rows."""

    def get(
        self, db: Session, ticker: str, interval: str, time_period: int,
    ) -> Optional[MarketAtrCache]:
        return (
            db.query(MarketAtrCache)
            .filter(MarketAtrCache.ticker == ticker)
            .filter(MarketAtrCache.interval == interval)
            .filter(MarketAtrCache.time_period == time_period)
            .first()
        )

    def upsert(
        self,
        db: Session,
        ticker: str,
        interval: str,
        time_period: int,
        *,
        atr_value: Optional[float],
        atr_date: Optional[date],
    ) -> MarketAtrCache:
        row = self.get(db, ticker, interval, time_period)
        if row is None:
            row = MarketAtrCache(
                ticker=ticker, interval=interval, time_period=time_period,
            )
            db.add(row)
        row.atr_value = atr_value
        row.atr_date = atr_date
        row.retrieved_at = datetime.now()
        return row

    def load_for_tickers(
        self, db: Session, tickers: set[str],
    ) -> dict[str, dict[tuple[str, int], Optional[float]]]:
        """Load ATR cache: {ticker: {(interval, period): atr_value}}"""
        if not tickers:
            return {}
        rows = (
            db.query(MarketAtrCache)
            .filter(MarketAtrCache.ticker.in_(tickers))
            .all()
        )
        result: dict[str, dict[tuple[str, int], Optional[float]]] = {}
        for row in rows:
            ticker_signals = result.setdefault(row.ticker, {})
            ticker_signals[(row.interval, row.time_period)] = row.atr_value
        return result


class WeeklyBarCacheRepository:
    """CRUD for :class:`MarketWeeklyBarCache` rows."""

    def get_latest_rows(
        self, db: Session, ticker: str, limit: int,
    ) -> list[MarketWeeklyBarCache]:
        return (
            db.query(MarketWeeklyBarCache)
            .filter(MarketWeeklyBarCache.ticker == ticker)
            .order_by(MarketWeeklyBarCache.bar_date.desc())
            .limit(limit)
            .all()
        )

    def get_all_for_ticker(
        self, db: Session, ticker: str,
    ) -> list[MarketWeeklyBarCache]:
        return (
            db.query(MarketWeeklyBarCache)
            .filter(MarketWeeklyBarCache.ticker == ticker)
            .all()
        )

    def upsert_bars(
        self, db: Session, ticker: str, bars: list, lookback_weeks: int,
    ) -> None:
        """Upsert weekly bars and trim old ones beyond lookback window."""
        target_friday = last_completed_trading_week_end()
        completed = [b for b in bars if b.date <= target_friday]
        completed.sort(key=lambda b: b.date, reverse=True)
        keep = completed[:lookback_weeks]

        existing_rows = self.get_all_for_ticker(db, ticker)
        rows_by_date = {row.bar_date: row for row in existing_rows}

        keep_dates = {b.date for b in keep}
        now = datetime.now()
        for bar in keep:
            row = rows_by_date.get(bar.date)
            if row is None:
                row = MarketWeeklyBarCache(ticker=ticker, bar_date=bar.date)
                db.add(row)
            row.open = bar.open
            row.high = bar.high
            row.low = bar.low
            row.close = bar.close
            row.volume = bar.volume
            row.retrieved_at = now

        for existing_date, row in rows_by_date.items():
            if existing_date not in keep_dates:
                db.delete(row)

    def load_for_tickers(
        self, db: Session, tickers: set[str],
    ) -> dict[str, list[MarketWeeklyBarCache]]:
        """Load weekly bars: {ticker: [bar, …]} sorted most-recent first."""
        if not tickers:
            return {}
        rows = (
            db.query(MarketWeeklyBarCache)
            .filter(MarketWeeklyBarCache.ticker.in_(tickers))
            .order_by(MarketWeeklyBarCache.ticker, MarketWeeklyBarCache.bar_date.desc())
            .all()
        )
        result: dict[str, list[MarketWeeklyBarCache]] = {}
        for row in rows:
            result.setdefault(row.ticker, []).append(row)
        return result


class DailyBarCacheRepository:
    """CRUD for :class:`MarketDailyBarCache` rows."""

    def get_latest(self, db: Session, ticker: str) -> Optional[MarketDailyBarCache]:
        return (
            db.query(MarketDailyBarCache)
            .filter(MarketDailyBarCache.ticker == ticker)
            .order_by(MarketDailyBarCache.bar_date.desc())
            .first()
        )

    def count_for_ticker(self, db: Session, ticker: str) -> int:
        return (
            db.query(MarketDailyBarCache)
            .filter(MarketDailyBarCache.ticker == ticker)
            .count()
        )

    def upsert_bars(
        self, db: Session, ticker: str, bars: list, lookback_days: int,
    ) -> None:
        """Upsert daily bars and trim old ones beyond lookback window.

        Stores ``lookback_days + 1`` bars so that period-return calculations
        have the start-of-window close available.
        """
        target_day = last_completed_trading_day()
        completed = [b for b in bars if b.date <= target_day]
        completed.sort(key=lambda b: b.date, reverse=True)
        keep = completed[: lookback_days + 1]

        existing_rows = (
            db.query(MarketDailyBarCache)
            .filter(MarketDailyBarCache.ticker == ticker)
            .all()
        )
        rows_by_date = {row.bar_date: row for row in existing_rows}
        keep_dates = {b.date for b in keep}
        now = datetime.now()
        for bar in keep:
            row = rows_by_date.get(bar.date)
            if row is None:
                row = MarketDailyBarCache(ticker=ticker, bar_date=bar.date)
                db.add(row)
            row.close = bar.close
            row.retrieved_at = now

        for existing_date, row in rows_by_date.items():
            if existing_date not in keep_dates:
                db.delete(row)

    def load_for_tickers(
        self, db: Session, tickers: set[str],
    ) -> dict[str, list[MarketDailyBarCache]]:
        """Load daily bars: {ticker: [bar, …]} sorted most-recent first."""
        if not tickers:
            return {}
        rows = (
            db.query(MarketDailyBarCache)
            .filter(MarketDailyBarCache.ticker.in_(tickers))
            .order_by(MarketDailyBarCache.ticker, MarketDailyBarCache.bar_date.desc())
            .all()
        )
        result: dict[str, list[MarketDailyBarCache]] = {}
        for row in rows:
            result.setdefault(row.ticker, []).append(row)
        return result
