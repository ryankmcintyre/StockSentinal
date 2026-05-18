"""Market data service layer.

Orchestrates provider calls, staleness checks, and cache persistence.
This is the single entry point that routes and background tasks should
use for anything market-data related.
"""

import logging
from datetime import date as date_type
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.alpha_vantage_client import (
    ATRPoint,
    DailyBar,
    SymbolSearchMatch,
    WeeklyBar,
)
from app.market_data.exceptions import MarketDataError
from app.models import Position
from app.schemas import InvestmentType
from app.unit_of_work import as_uow

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


def _compute_sma(closes: list[float], period: int) -> Optional[float]:
    """Compute a simple moving average from a list of close prices.

    *closes* should be ordered most-recent-first. Returns None if
    fewer than *period* values are available.
    """
    if len(closes) < period:
        return None
    return sum(closes[:period]) / period


class _FetchCache:
    """Per-operation cache for API responses.

    Created at the start of a refresh operation and passed to sub-methods
    so that each API endpoint is called at most once per ticker per
    operation, regardless of how many sub-steps need the data.
    """

    def __init__(self, provider: MarketDataProvider) -> None:
        self._provider = provider
        self._daily_bars: dict[str, list[DailyBar]] = {}
        self._weekly_bars: dict[str, list[WeeklyBar]] = {}
        self._atr: dict[tuple[str, str, int], list[ATRPoint]] = {}

    def get_daily_bars(self, ticker: str) -> list[DailyBar]:
        key = ticker.upper()
        if key not in self._daily_bars:
            self._daily_bars[key] = self._provider.fetch_daily_bars(ticker)
        return self._daily_bars[key]

    def preload_daily_bars(self, tickers: set[str]) -> None:
        self._preload_bars(tickers, "fetch_daily_bars_batch", self._daily_bars)

    def get_weekly_bars(self, ticker: str) -> list[WeeklyBar]:
        key = ticker.upper()
        if key not in self._weekly_bars:
            self._weekly_bars[key] = self._provider.fetch_weekly_bars(ticker)
        return self._weekly_bars[key]

    def preload_weekly_bars(self, tickers: set[str]) -> None:
        self._preload_bars(tickers, "fetch_weekly_bars_batch", self._weekly_bars)

    def _preload_bars(
        self,
        tickers: set[str],
        method_name: str,
        target_cache: dict[str, list[DailyBar] | list[WeeklyBar]],
    ) -> None:
        missing = sorted(ticker for ticker in tickers if ticker.upper() not in target_cache)
        if not missing:
            return
        if not getattr(type(self._provider), "supports_batch_fetch", False):
            return
        batch_method = getattr(self._provider, method_name, None)
        if not callable(batch_method):
            return
        try:
            fetched = batch_method(missing)
        except Exception:
            logger.exception("Batch market data preload failed for %s", method_name)
            return
        for ticker, bars in fetched.items():
            target_cache[ticker.upper()] = bars

    def get_atr(self, ticker: str, interval: str, time_period: int) -> list[ATRPoint]:
        key = (ticker, interval, time_period)
        if key not in self._atr:
            self._atr[key] = self._provider.fetch_atr(
                ticker, interval=interval, time_period=time_period,
            )
        return self._atr[key]

    def compute_daily_sma(self, ticker: str, period: int) -> Optional[float]:
        """Compute SMA from cached daily bars, avoiding an API call."""
        bars = self.get_daily_bars(ticker)
        closes = [b.close for b in bars]
        return _compute_sma(closes, period)

    def compute_weekly_sma(
        self, ticker: str, period: int, as_of: Optional[date_type] = None,
    ) -> Optional[float]:
        """Compute SMA from cached weekly bars, avoiding an API call.

        If *as_of* is provided, only bars on or before that date are
        considered (used to align with the last completed trading week).
        """
        bars = self.get_weekly_bars(ticker)
        if as_of:
            bars = [b for b in bars if b.date <= as_of]
        closes = [b.close for b in bars]
        return _compute_sma(closes, period)


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

    def fetch_ticker_matches(self, symbol: str) -> list[SymbolSearchMatch]:
        """Rate-limited ticker match lookup via the provider."""
        return self._provider.fetch_ticker_matches(symbol)

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
        close_cache: dict[tuple[str, str], tuple[Optional[float], Optional[date_type]]],
        fetch_cache: Optional["_FetchCache"] = None,
    ) -> None:
        """Fetch and upsert one indicator cache entry.

        When *fetch_cache* is provided, bars and SMA are computed locally
        from already-fetched data, avoiding redundant API calls.
        """
        close_key = (ticker, interval)
        if close_key not in close_cache:
            if interval == "daily":
                bars = (
                    fetch_cache.get_daily_bars(ticker)
                    if fetch_cache
                    else self._provider.fetch_daily_bars(ticker)
                )
                if bars:
                    close_cache[close_key] = (bars[0].close, bars[0].date)
            elif interval == "weekly":
                bars = (
                    fetch_cache.get_weekly_bars(ticker)
                    if fetch_cache
                    else self._provider.fetch_weekly_bars(ticker)
                )
                if bars:
                    target_friday = last_completed_trading_week_end()
                    bar = bars[0]
                    if bar.date > target_friday and len(bars) > 1:
                        bar = bars[1]
                    close_cache[close_key] = (bar.close, bar.date)

        close_val, close_date = close_cache.get(close_key, (None, None))

        sma_val: Optional[float] = None
        sma_date: Optional[date_type] = None

        if fetch_cache:
            # Compute SMA locally from cached bars
            if interval == "daily":
                sma_val = fetch_cache.compute_daily_sma(ticker, time_period)
                if sma_val is not None:
                    bars = fetch_cache.get_daily_bars(ticker)
                    sma_date = bars[0].date if bars else None
            else:
                target_friday = last_completed_trading_week_end()
                sma_val = fetch_cache.compute_weekly_sma(
                    ticker, time_period, as_of=target_friday,
                )
                if sma_val is not None:
                    sma_date = target_friday
        else:
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
        fetch_cache: Optional["_FetchCache"] = None,
    ) -> list[str]:
        """Refresh the indicator cache for given tickers and indicators.

        Returns list of error messages (empty on full success).
        """
        if not tickers or not required_indicators:
            return []

        errors: list[str] = []
        close_cache: dict[tuple[str, str], tuple[Optional[float], Optional[date_type]]] = {}

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
                        fetch_cache=fetch_cache,
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
        fetch_cache: Optional["_FetchCache"] = None,
    ) -> None:
        """Fetch and upsert one ATR cache entry."""
        points = (
            fetch_cache.get_atr(ticker, interval, time_period)
            if fetch_cache
            else self._provider.fetch_atr(
                ticker, interval=interval, time_period=time_period,
            )
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
        fetch_cache: Optional["_FetchCache"] = None,
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
                        fetch_cache=fetch_cache,
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
        fetch_cache: Optional["_FetchCache"] = None,
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
                bars = (
                    fetch_cache.get_weekly_bars(ticker)
                    if fetch_cache
                    else self._provider.fetch_weekly_bars(ticker)
                )
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
        fetch_cache: Optional["_FetchCache"] = None,
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
                bars = (
                    fetch_cache.get_daily_bars(ticker)
                    if fetch_cache
                    else self._provider.fetch_daily_bars(ticker)
                )
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

    def _refresh_daily(
        self, position: Position, fetch_cache: Optional["_FetchCache"] = None,
    ) -> None:
        """Fetch and cache daily close + SMA-21 for a position."""
        symbol = position.ticker
        logger.info("Refreshing daily data for %s", symbol)

        bars = (
            fetch_cache.get_daily_bars(symbol)
            if fetch_cache
            else self._provider.fetch_daily_bars(symbol)
        )
        if not bars:
            raise MarketDataError(f"No daily bars returned for {symbol}")

        latest_bar = bars[0]
        position.daily_close = latest_bar.close
        position.daily_market_date = latest_bar.date

        # Compute SMA-21 locally from bars to avoid an extra API call
        sma_21 = _compute_sma([b.close for b in bars], 21)
        if sma_21 is not None:
            position.daily_sma_21 = sma_21
        elif not fetch_cache:
            # Fallback to API only if we don't have a cache (shouldn't happen
            # with compact output returning ~100 bars)
            sma_points = self._provider.fetch_sma(
                symbol, interval="daily", time_period=21,
            )
            if sma_points:
                position.daily_sma_21 = sma_points[0].sma

        position.daily_retrieved_at = datetime.now()

    def _refresh_weekly(
        self, position: Position, fetch_cache: Optional["_FetchCache"] = None,
    ) -> None:
        """Fetch and cache weekly close + SMA-20 for a position."""
        symbol = position.ticker
        logger.info("Refreshing weekly data for %s", symbol)

        bars = (
            fetch_cache.get_weekly_bars(symbol)
            if fetch_cache
            else self._provider.fetch_weekly_bars(symbol)
        )
        if not bars:
            raise MarketDataError(f"No weekly bars returned for {symbol}")

        target_friday = last_completed_trading_week_end()
        latest_bar = bars[0]
        if latest_bar.date > target_friday and len(bars) > 1:
            latest_bar = bars[1]

        position.weekly_close = latest_bar.close
        position.weekly_market_date = latest_bar.date

        # Compute SMA-20 locally from weekly bars up to the target week
        completed_bars = [b for b in bars if b.date <= target_friday]
        sma_20 = _compute_sma([b.close for b in completed_bars], 20)
        if sma_20 is not None:
            position.weekly_sma_20 = sma_20
        elif not fetch_cache:
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

        Uses a per-operation fetch cache so that each API endpoint is called
        at most once per ticker, regardless of how many sub-steps need the data.
        """
        errors: list[str] = []
        cache = _FetchCache(self._provider)

        # Daily refresh
        if force or daily_data_is_stale(position):
            logger.debug("%s daily data is stale, refreshing", position.ticker)
            try:
                self._refresh_daily(position, fetch_cache=cache)
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
                    self._refresh_weekly(position, fetch_cache=cache)
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

        rule_uow = as_uow(db)
        required = rule_config.get_required_indicators(rule_uow)
        if required:
            cache_errors = self.refresh_indicator_cache(
                db, {position.ticker}, required, force=force,
                fetch_cache=cache,
            )
            errors.extend(cache_errors)

        required_atr = rule_config.get_required_atr_indicators(rule_uow)
        if required_atr:
            atr_errors = self.refresh_atr_cache(
                db, {position.ticker}, required_atr, force=force,
                fetch_cache=cache,
            )
            errors.extend(atr_errors)

        weekly_lookback = rule_config.get_required_weekly_bar_lookback(rule_uow)
        if weekly_lookback > 0:
            weekly_bar_errors = self.refresh_weekly_bar_cache(
                db, {position.ticker}, weekly_lookback, force=force,
                fetch_cache=cache,
            )
            errors.extend(weekly_bar_errors)

        daily_lookback = rule_config.get_required_daily_bar_lookback(rule_uow)
        if daily_lookback > 0:
            benchmark = getattr(position, "sector_benchmark_ticker", None)
            if benchmark:
                daily_tickers: set[str] = {position.ticker, benchmark.upper()}
                daily_bar_errors = self.refresh_daily_bar_cache(
                    db, daily_tickers, daily_lookback, force=force,
                    fetch_cache=cache,
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

        # Single fetch cache shared across the entire refresh-all operation
        cache = _FetchCache(self._provider)

        # Group positions by ticker
        ticker_groups: dict[str, list[Position]] = {}
        for pos in positions:
            ticker_groups.setdefault(pos.ticker, []).append(pos)

        refresh_plan = {}
        daily_batch_tickers: set[str] = set()
        weekly_batch_tickers: set[str] = set()

        for ticker, group in ticker_groups.items():
            daily_needs = {id(p): force or daily_data_is_stale(p) for p in group}
            weekly_needs = {
                id(p): (force or weekly_data_is_stale(p)) and self._needs_weekly(p)
                for p in group
            }
            group_needs_daily = any(daily_needs.values())
            group_needs_weekly = any(weekly_needs.values())

            representative = group[0]
            if group_needs_weekly:
                for p in group:
                    if self._needs_weekly(p):
                        representative = p
                        break

            refresh_plan[ticker] = {
                "daily_needs": daily_needs,
                "weekly_needs": weekly_needs,
                "group_needs_daily": group_needs_daily,
                "group_needs_weekly": group_needs_weekly,
                "representative": representative,
            }
            if group_needs_daily:
                daily_batch_tickers.add(ticker)
            if group_needs_weekly:
                weekly_batch_tickers.add(ticker)

        cache.preload_daily_bars(daily_batch_tickers)
        cache.preload_weekly_bars(weekly_batch_tickers)

        refreshed = 0
        for ticker, group in ticker_groups.items():
            plan = refresh_plan[ticker]
            daily_needs = plan["daily_needs"]
            weekly_needs = plan["weekly_needs"]
            group_needs_daily = plan["group_needs_daily"]
            group_needs_weekly = plan["group_needs_weekly"]

            if not group_needs_daily and not group_needs_weekly:
                continue

            representative = plan["representative"]

            errors: list[str] = []

            daily_ok = False
            if group_needs_daily:
                try:
                    self._refresh_daily(representative, fetch_cache=cache)
                    daily_ok = True
                except Exception as exc:
                    logger.exception("Daily refresh failed for %s", ticker)
                    errors.append(f"Daily refresh failed: {exc}")

            weekly_ok = False
            if group_needs_weekly:
                try:
                    self._refresh_weekly(representative, fetch_cache=cache)
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

        rule_uow = as_uow(db)
        required = rule_config.get_required_indicators(rule_uow)
        if required and ticker_groups:
            all_tickers = set(ticker_groups.keys())
            cache_errors = self.refresh_indicator_cache(
                db, all_tickers, required, force=force,
                fetch_cache=cache,
            )
            if cache_errors:
                logger.warning("Indicator cache refresh errors: %s", cache_errors)

        required_atr = rule_config.get_required_atr_indicators(rule_uow)
        if required_atr and ticker_groups:
            all_tickers = set(ticker_groups.keys())
            atr_errors = self.refresh_atr_cache(
                db, all_tickers, required_atr, force=force,
                fetch_cache=cache,
            )
            if atr_errors:
                logger.warning("ATR cache refresh errors: %s", atr_errors)

        weekly_lookback = rule_config.get_required_weekly_bar_lookback(rule_uow)
        if weekly_lookback > 0 and ticker_groups:
            all_tickers = set(ticker_groups.keys())
            bar_errors = self.refresh_weekly_bar_cache(
                db, all_tickers, weekly_lookback, force=force,
                fetch_cache=cache,
            )
            if bar_errors:
                logger.warning("Weekly bar cache refresh errors: %s", bar_errors)

        daily_lookback = rule_config.get_required_daily_bar_lookback(rule_uow)
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
                    fetch_cache=cache,
                )
                if daily_errors:
                    logger.warning("Daily bar cache refresh errors: %s", daily_errors)

        logger.info(
            "Refresh complete: %d/%d positions refreshed",
            refreshed, len(positions),
        )
        return refreshed
