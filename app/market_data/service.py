"""Market data service layer.

Orchestrates provider calls, staleness checks, and cache persistence.
This is the single entry point that routes and background tasks should
use for anything market-data related.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.alpha_vantage_client import AlphaVantageError, DailyBar
from app.models import Position
from app.schemas import InvestmentType

from .cache_repos import (
    AtrCacheRepository,
    DailyBarCacheRepository,
    IndicatorCacheRepository,
    WeeklyBarCacheRepository,
)
from .provider import MarketDataProvider
from .staleness import (
    atr_cache_is_stale,
    daily_bar_cache_is_stale,
    daily_data_is_stale,
    indicator_cache_is_stale,
    last_completed_trading_week_end,
    weekly_bar_cache_is_stale,
    weekly_data_is_stale,
)

logger = logging.getLogger(__name__)


class MarketDataService:
    """Central orchestrator for market data fetching and caching.

    Constructed with a :class:`MarketDataProvider` and optional cache
    repository overrides (defaults are created automatically).
    """

    def __init__(
        self,
        provider: MarketDataProvider,
        *,
        indicator_repo: Optional[IndicatorCacheRepository] = None,
        atr_repo: Optional[AtrCacheRepository] = None,
        weekly_bar_repo: Optional[WeeklyBarCacheRepository] = None,
        daily_bar_repo: Optional[DailyBarCacheRepository] = None,
    ) -> None:
        self._provider = provider
        self._indicator_repo = indicator_repo or IndicatorCacheRepository()
        self._atr_repo = atr_repo or AtrCacheRepository()
        self._weekly_bar_repo = weekly_bar_repo or WeeklyBarCacheRepository()
        self._daily_bar_repo = daily_bar_repo or DailyBarCacheRepository()

    # -- Convenience pass-throughs to provider ------------------------------

    def fetch_company_name(self, symbol: str) -> str:
        """Rate-limited company name lookup via the provider."""
        return self._provider.fetch_company_name(symbol)

    def fetch_daily_series(self, symbol: str) -> list[DailyBar]:
        """Rate-limited daily bar fetch via the provider."""
        return self._provider.fetch_daily_bars(symbol)

    # -- Cache loaders (read-only) ------------------------------------------

    def load_indicator_cache_for_tickers(self, db: Session, tickers: set[str]):
        return self._indicator_repo.load_for_tickers(db, tickers)

    def load_atr_cache_for_tickers(self, db: Session, tickers: set[str]):
        return self._atr_repo.load_for_tickers(db, tickers)

    def load_weekly_bar_cache_for_tickers(self, db: Session, tickers: set[str]):
        return self._weekly_bar_repo.load_for_tickers(db, tickers)

    def load_daily_bar_cache_for_tickers(self, db: Session, tickers: set[str]):
        return self._daily_bar_repo.load_for_tickers(db, tickers)

    # -- Indicator cache refresh --------------------------------------------

    def _refresh_indicator_cache_entry(
        self,
        db: Session,
        ticker: str,
        interval: str,
        time_period: int,
        close_cache: dict[tuple[str, str], tuple[float, "date"]],
    ) -> None:
        """Fetch and upsert one indicator cache entry."""
        close_key = (ticker, interval)
        if close_key not in close_cache:
            if interval == "daily":
                bars = self._provider.fetch_daily_bars(ticker)
                if bars:
                    close_cache[close_key] = (bars[0].close, bars[0].date)
            elif interval == "weekly":
                bars = self._provider.fetch_weekly_bars(ticker)
                if bars:
                    target_friday = last_completed_trading_week_end()
                    bar = bars[0]
                    if bar.date > target_friday and len(bars) > 1:
                        bar = bars[1]
                    close_cache[close_key] = (bar.close, bar.date)

        close_val, close_date = close_cache.get(close_key, (None, None))

        sma_val = None
        sma_date = None
        sma_points = self._provider.fetch_sma(
            ticker, interval=interval, time_period=time_period,
        )
        if sma_points:
            if interval == "weekly":
                target_friday = last_completed_trading_week_end()
                for pt in sma_points:
                    if pt.date <= target_friday:
                        sma_val = pt.sma
                        sma_date = pt.date
                        break
                else:
                    sma_val = sma_points[0].sma
                    sma_date = sma_points[0].date
            else:
                sma_val = sma_points[0].sma
                sma_date = sma_points[0].date

        self._indicator_repo.upsert(
            db, ticker, interval, time_period,
            sma_value=sma_val, sma_date=sma_date,
            close_value=close_val, close_date=close_date,
        )

    def refresh_indicator_cache(
        self,
        db: Session,
        tickers: set[str],
        required_indicators: set[tuple[str, int]],
        force: bool = False,
    ) -> list[str]:
        """Refresh the indicator cache for given tickers and indicators.

        Returns list of error messages (empty on full success).
        """
        if not tickers or not required_indicators:
            return []

        errors: list[str] = []
        close_cache: dict[tuple[str, str], tuple[float, "date"]] = {}

        for ticker in sorted(tickers):
            for interval, time_period in sorted(required_indicators):
                if not force:
                    existing = self._indicator_repo.get(
                        db, ticker, interval, time_period,
                    )
                    if not indicator_cache_is_stale(existing, interval):
                        continue
                try:
                    self._refresh_indicator_cache_entry(
                        db, ticker, interval, time_period, close_cache,
                    )
                except Exception as exc:
                    msg = f"Indicator cache refresh failed for {ticker} {interval} SMA-{time_period}: {exc}"
                    logger.exception(msg)
                    errors.append(msg)

        db.commit()
        return errors

    # -- ATR cache refresh --------------------------------------------------

    def _refresh_atr_cache_entry(
        self, db: Session, ticker: str, interval: str, time_period: int,
    ) -> None:
        """Fetch and upsert one ATR cache entry."""
        points = self._provider.fetch_atr(
            ticker, interval=interval, time_period=time_period,
        )
        atr_val: Optional[float] = None
        atr_date = None
        if points:
            if interval == "weekly":
                target_friday = last_completed_trading_week_end()
                for pt in points:
                    if pt.date <= target_friday:
                        atr_val = pt.atr
                        atr_date = pt.date
                        break
                else:
                    atr_val = points[0].atr
                    atr_date = points[0].date
            else:
                atr_val = points[0].atr
                atr_date = points[0].date

        self._atr_repo.upsert(
            db, ticker, interval, time_period,
            atr_value=atr_val, atr_date=atr_date,
        )

    def refresh_atr_cache(
        self,
        db: Session,
        tickers: set[str],
        required_atr_indicators: set[tuple[str, int]],
        force: bool = False,
    ) -> list[str]:
        """Refresh the ATR cache. Returns list of error messages."""
        if not tickers or not required_atr_indicators:
            return []

        errors: list[str] = []
        for ticker in sorted(tickers):
            for interval, time_period in sorted(required_atr_indicators):
                if not force:
                    existing = self._atr_repo.get(
                        db, ticker, interval, time_period,
                    )
                    if not atr_cache_is_stale(existing, interval):
                        continue
                try:
                    self._refresh_atr_cache_entry(
                        db, ticker, interval, time_period,
                    )
                except Exception as exc:
                    msg = f"ATR cache refresh failed for {ticker} {interval} ATR-{time_period}: {exc}"
                    logger.exception(msg)
                    errors.append(msg)

        db.commit()
        return errors

    # -- Weekly bar cache refresh -------------------------------------------

    def refresh_weekly_bar_cache(
        self,
        db: Session,
        tickers: set[str],
        lookback_weeks: int,
        force: bool = False,
    ) -> list[str]:
        """Refresh the weekly OHLC bar cache. Returns list of error messages."""
        if not tickers or lookback_weeks <= 0:
            return []

        errors: list[str] = []
        for ticker in sorted(tickers):
            if not force:
                cached_rows = self._weekly_bar_repo.get_latest_rows(
                    db, ticker, lookback_weeks,
                )
                latest = cached_rows[0] if cached_rows else None
                has_required_history = len(cached_rows) >= lookback_weeks
                if has_required_history and not weekly_bar_cache_is_stale(latest):
                    continue

            try:
                bars = self._provider.fetch_weekly_bars(ticker)
                if bars:
                    self._weekly_bar_repo.upsert_bars(
                        db, ticker, bars, lookback_weeks,
                    )
            except Exception as exc:
                msg = f"Weekly bar cache refresh failed for {ticker}: {exc}"
                logger.exception(msg)
                errors.append(msg)

        db.commit()
        return errors

    # -- Daily bar cache refresh --------------------------------------------

    def refresh_daily_bar_cache(
        self,
        db: Session,
        tickers: set[str],
        lookback_days: int,
        force: bool = False,
    ) -> list[str]:
        """Refresh the daily close bar cache. Returns list of error messages."""
        if not tickers or lookback_days <= 0:
            return []

        errors: list[str] = []
        expected_row_count = lookback_days + 1

        for ticker in sorted(tickers):
            if not force:
                latest = self._daily_bar_repo.get_latest(db, ticker)
                cached_row_count = self._daily_bar_repo.count_for_ticker(db, ticker)
                if cached_row_count == expected_row_count and not daily_bar_cache_is_stale(latest):
                    continue

            try:
                bars = self._provider.fetch_daily_bars(ticker)
                if bars:
                    self._daily_bar_repo.upsert_bars(
                        db, ticker, bars, lookback_days,
                    )
            except Exception as exc:
                msg = f"Daily bar cache refresh failed for {ticker}: {exc}"
                logger.exception(msg)
                errors.append(msg)

        db.commit()
        return errors

    # -- Position refresh helpers -------------------------------------------

    @staticmethod
    def _needs_weekly(position: Position) -> bool:
        return position.investment_type == InvestmentType.long_term

    def _refresh_daily(self, position: Position) -> None:
        """Fetch and cache daily close + SMA-21 for a position."""
        symbol = position.ticker
        logger.info("Refreshing daily data for %s", symbol)

        bars = self._provider.fetch_daily_bars(symbol)
        if not bars:
            raise AlphaVantageError(f"No daily bars returned for {symbol}")

        latest_bar = bars[0]
        position.daily_close = latest_bar.close
        position.daily_market_date = latest_bar.date

        sma_points = self._provider.fetch_sma(
            symbol, interval="daily", time_period=21,
        )
        if sma_points:
            position.daily_sma_21 = sma_points[0].sma

        position.daily_retrieved_at = datetime.now()

    def _refresh_weekly(self, position: Position) -> None:
        """Fetch and cache weekly close + SMA-20 for a position."""
        symbol = position.ticker
        logger.info("Refreshing weekly data for %s", symbol)

        bars = self._provider.fetch_weekly_bars(symbol)
        if not bars:
            raise AlphaVantageError(f"No weekly bars returned for {symbol}")

        target_friday = last_completed_trading_week_end()
        latest_bar = bars[0]
        if latest_bar.date > target_friday and len(bars) > 1:
            latest_bar = bars[1]

        position.weekly_close = latest_bar.close
        position.weekly_market_date = latest_bar.date

        sma_points = self._provider.fetch_sma(
            symbol, interval="weekly", time_period=20,
        )
        if sma_points:
            for pt in sma_points:
                if pt.date <= target_friday:
                    position.weekly_sma_20 = pt.sma
                    break
            else:
                position.weekly_sma_20 = sma_points[0].sma

        position.weekly_retrieved_at = datetime.now()

    @staticmethod
    def _copy_daily_cache(source: Position, target: Position) -> None:
        target.daily_close = source.daily_close
        target.daily_sma_21 = source.daily_sma_21
        target.daily_market_date = source.daily_market_date
        target.daily_retrieved_at = source.daily_retrieved_at

    @staticmethod
    def _copy_weekly_cache(source: Position, target: Position) -> None:
        target.weekly_close = source.weekly_close
        target.weekly_sma_20 = source.weekly_sma_20
        target.weekly_market_date = source.weekly_market_date
        target.weekly_retrieved_at = source.weekly_retrieved_at

    # -- Main refresh orchestration -----------------------------------------

    def refresh_position(
        self, position: Position, db: Session, force: bool = False,
    ) -> None:
        """Refresh cached market data for a single position.

        Respects staleness checks unless *force* is True. Weekly data is
        only fetched for long-term positions. Errors are stored in
        ``position.refresh_error`` so the UI can report them.

        Also refreshes the indicator / ATR / bar caches for configured rules.
        """
        errors: list[str] = []

        # Daily refresh
        if force or daily_data_is_stale(position):
            logger.debug("%s daily data is stale, refreshing", position.ticker)
            try:
                self._refresh_daily(position)
            except Exception as exc:
                logger.exception("Daily refresh failed for %s", position.ticker)
                errors.append(f"Daily refresh failed: {exc}")
        else:
            logger.debug("%s daily data is fresh, skipping", position.ticker)

        # Weekly refresh (long-term only)
        if self._needs_weekly(position):
            if force or weekly_data_is_stale(position):
                logger.debug("%s weekly data is stale, refreshing", position.ticker)
                try:
                    self._refresh_weekly(position)
                except Exception as exc:
                    logger.exception(
                        "Weekly refresh failed for %s: %s", position.ticker, exc,
                    )
                    errors.append(f"Weekly refresh failed: {exc}")
            else:
                logger.debug("%s weekly data is fresh, skipping", position.ticker)
        else:
            logger.debug(
                "%s is short-term, skipping Position weekly snapshot refresh",
                position.ticker,
            )

        # Refresh indicator caches for configured rules
        import app.rule_config as rule_config

        required = rule_config.get_required_indicators(db)
        if required:
            cache_errors = self.refresh_indicator_cache(
                db, {position.ticker}, required, force=force,
            )
            errors.extend(cache_errors)

        required_atr = rule_config.get_required_atr_indicators(db)
        if required_atr:
            atr_errors = self.refresh_atr_cache(
                db, {position.ticker}, required_atr, force=force,
            )
            errors.extend(atr_errors)

        weekly_lookback = rule_config.get_required_weekly_bar_lookback(db)
        if weekly_lookback > 0:
            weekly_bar_errors = self.refresh_weekly_bar_cache(
                db, {position.ticker}, weekly_lookback, force=force,
            )
            errors.extend(weekly_bar_errors)

        daily_lookback = rule_config.get_required_daily_bar_lookback(db)
        if daily_lookback > 0:
            benchmark = getattr(position, "sector_benchmark_ticker", None)
            if benchmark:
                daily_tickers: set[str] = {position.ticker, benchmark.upper()}
                daily_bar_errors = self.refresh_daily_bar_cache(
                    db, daily_tickers, daily_lookback, force=force,
                )
                errors.extend(daily_bar_errors)

        position.refresh_error = "; ".join(errors) if errors else None
        db.commit()

    def refresh_all_positions(
        self, db: Session, force: bool = False,
    ) -> int:
        """Refresh cached market data for all positions.

        Deduplicates API calls: positions sharing the same ticker are grouped
        so that each unique ticker is fetched at most once.

        Also refreshes indicator / ATR / bar caches for configured rules.

        Returns the number of positions that were actually refreshed.
        """
        positions = db.query(Position).all()
        logger.info(
            "Starting refresh for %d positions (force=%s)",
            len(positions), force,
        )

        # Group positions by ticker
        ticker_groups: dict[str, list[Position]] = {}
        for pos in positions:
            ticker_groups.setdefault(pos.ticker, []).append(pos)

        refreshed = 0
        for ticker, group in ticker_groups.items():
            daily_needs = {id(p): force or daily_data_is_stale(p) for p in group}
            weekly_needs = {
                id(p): (force or weekly_data_is_stale(p)) and self._needs_weekly(p)
                for p in group
            }
            group_needs_daily = any(daily_needs.values())
            group_needs_weekly = any(weekly_needs.values())

            if not group_needs_daily and not group_needs_weekly:
                continue

            representative = group[0]
            if group_needs_weekly:
                for p in group:
                    if self._needs_weekly(p):
                        representative = p
                        break

            errors: list[str] = []

            daily_ok = False
            if group_needs_daily:
                try:
                    self._refresh_daily(representative)
                    daily_ok = True
                except Exception as exc:
                    logger.exception("Daily refresh failed for %s", ticker)
                    errors.append(f"Daily refresh failed: {exc}")

            weekly_ok = False
            if group_needs_weekly:
                try:
                    self._refresh_weekly(representative)
                    weekly_ok = True
                except Exception as exc:
                    logger.exception("Weekly refresh failed for %s", ticker)
                    errors.append(f"Weekly refresh failed: {exc}")

            representative.refresh_error = "; ".join(errors) if errors else None

            for pos in group:
                if pos is representative:
                    refreshed += 1
                    continue
                copied_daily = group_needs_daily and daily_ok
                copied_weekly = group_needs_weekly and self._needs_weekly(pos) and weekly_ok

                if copied_daily:
                    self._copy_daily_cache(representative, pos)
                if copied_weekly:
                    self._copy_weekly_cache(representative, pos)

                required_refresh_succeeded = False
                if daily_needs[id(pos)] or weekly_needs[id(pos)]:
                    required_refresh_succeeded = (
                        (not daily_needs[id(pos)] or daily_ok)
                        and (not weekly_needs[id(pos)] or weekly_ok)
                    )
                if required_refresh_succeeded:
                    pos.refresh_error = None
                refreshed += 1

            db.commit()

        # Refresh indicator / ATR / bar caches for configured rules
        import app.rule_config as rule_config

        required = rule_config.get_required_indicators(db)
        if required and ticker_groups:
            all_tickers = set(ticker_groups.keys())
            cache_errors = self.refresh_indicator_cache(
                db, all_tickers, required, force=force,
            )
            if cache_errors:
                logger.warning("Indicator cache refresh errors: %s", cache_errors)

        required_atr = rule_config.get_required_atr_indicators(db)
        if required_atr and ticker_groups:
            all_tickers = set(ticker_groups.keys())
            atr_errors = self.refresh_atr_cache(
                db, all_tickers, required_atr, force=force,
            )
            if atr_errors:
                logger.warning("ATR cache refresh errors: %s", atr_errors)

        weekly_lookback = rule_config.get_required_weekly_bar_lookback(db)
        if weekly_lookback > 0 and ticker_groups:
            all_tickers = set(ticker_groups.keys())
            bar_errors = self.refresh_weekly_bar_cache(
                db, all_tickers, weekly_lookback, force=force,
            )
            if bar_errors:
                logger.warning("Weekly bar cache refresh errors: %s", bar_errors)

        daily_lookback = rule_config.get_required_daily_bar_lookback(db)
        if daily_lookback > 0 and ticker_groups:
            daily_tickers: set[str] = set()
            for pos in positions:
                benchmark = getattr(pos, "sector_benchmark_ticker", None)
                if benchmark:
                    daily_tickers.add(pos.ticker)
                    daily_tickers.add(benchmark.upper())
            if daily_tickers:
                daily_errors = self.refresh_daily_bar_cache(
                    db, daily_tickers, daily_lookback, force=force,
                )
                if daily_errors:
                    logger.warning("Daily bar cache refresh errors: %s", daily_errors)

        logger.info(
            "Refresh complete: %d/%d positions refreshed",
            refreshed, len(positions),
        )
        return refreshed
