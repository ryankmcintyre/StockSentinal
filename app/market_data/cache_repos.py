"""Cache repository classes.

Each repository encapsulates the SQLAlchemy queries for one cache table.
Repositories are stateless — they receive a ``Session`` on every call so
that callers control the transaction boundary.
"""

from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    MarketAtrCache,
    MarketDailyBarCache,
    MarketIndicatorCache,
    MarketWeeklyBarCache,
)

from .staleness import last_completed_trading_day, last_completed_trading_week_end


def _utc_now_for_storage() -> datetime:
    """Return the current UTC time normalized for plain DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bulk_upsert_bar_cache_rows(db: Session, table, ticker: str, bars: list) -> None:
    """Bulk upsert OHLCV cache rows for SQLite and PostgreSQL."""
    values_by_date = {}
    now = _utc_now_for_storage()
    for bar in bars:
        values_by_date[bar.date] = {
            "ticker": ticker,
            "bar_date": bar.date,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "retrieved_at": now,
        }

    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        insert_stmt = postgresql_insert(table)
    elif dialect_name == "sqlite":
        insert_stmt = sqlite_insert(table)
    else:
        raise NotImplementedError(f"Unsupported SQLAlchemy dialect for bar cache upsert: {dialect_name}")

    statement = insert_stmt.values(list(values_by_date.values()))
    excluded = statement.excluded
    db.execute(
        statement.on_conflict_do_update(
            index_elements=["ticker", "bar_date"],
            set_={
                "open": excluded.open,
                "high": excluded.high,
                "low": excluded.low,
                "close": excluded.close,
                "volume": excluded.volume,
                "retrieved_at": excluded.retrieved_at,
            },
        )
    )


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

    def upsert_bars(
        self, db: Session, ticker: str, bars: list, lookback_weeks: int,
    ) -> None:
        """Upsert weekly bars and trim old ones beyond lookback window."""
        target_friday = last_completed_trading_week_end()
        completed = [b for b in bars if b.date <= target_friday]
        completed.sort(key=lambda b: b.date, reverse=True)
        keep = completed[:lookback_weeks]

        if not keep:
            return

        # Batch delete: remove all bars for this ticker older than the cutoff.
        # No session sync needed — deleted rows are not referenced afterward.
        cutoff_date = keep[-1].date
        db.query(MarketWeeklyBarCache).filter(
            MarketWeeklyBarCache.ticker == ticker,
            MarketWeeklyBarCache.bar_date < cutoff_date,
        ).delete(synchronize_session=False)

        _bulk_upsert_bar_cache_rows(db, MarketWeeklyBarCache.__table__, ticker, keep)

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

        if not keep:
            return

        # Batch delete: remove all bars for this ticker older than the cutoff.
        # No session sync needed — deleted rows are not referenced afterward.
        cutoff_date = keep[-1].date
        db.query(MarketDailyBarCache).filter(
            MarketDailyBarCache.ticker == ticker,
            MarketDailyBarCache.bar_date < cutoff_date,
        ).delete(synchronize_session=False)

        _bulk_upsert_bar_cache_rows(db, MarketDailyBarCache.__table__, ticker, keep)

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
