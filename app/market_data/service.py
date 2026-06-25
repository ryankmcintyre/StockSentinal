"""Market data service layer.

Orchestrates provider calls, staleness checks, and cache persistence.
This is the single entry point that routes and background tasks should
use for anything market-data related.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_type
from datetime import datetime, timezone
from typing import Callable, Optional

import app.rule_config as rule_config
from sqlalchemy.orm import Session

from app.alpha_vantage_client import (
    ATRPoint,
    DailyBar,
    SymbolSearchMatch,
    WeeklyBar,
)
from app.market_data.exceptions import MarketDataError
from app.models import Position
from app.rule_engine import DailyClosePoint, MarketSignals, WeeklyOhlcBar, evaluate_position, get_verdict
from app.schemas import InvestmentType
from app.unit_of_work import as_uow

from .cache_repos import (
    AtrCacheRepository,
    DailyBarCacheRepository,
    IndicatorCacheRepository,
    WeeklyBarCacheRepository,
)
from .profiling import refresh_profiling_scope, time_block
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


def _compute_atr(
    bars: list[DailyBar] | list[WeeklyBar],
    period: int,
    as_of: Optional[date_type] = None,
) -> Optional[tuple[float, date_type]]:
    """Compute ATR from OHLC bars ordered most-recent-first."""
    if period <= 0:
        return None
    if as_of:
        bars = [bar for bar in bars if bar.date <= as_of]
    bars = sorted(bars, key=lambda bar: bar.date, reverse=True)
    if len(bars) < period + 1:
        return None

    chronological = list(reversed(bars))
    true_ranges: list[float] = []
    for index in range(1, len(chronological)):
        bar = chronological[index]
        previous_bar = chronological[index - 1]
        if (
            bar.high is None
            or bar.low is None
            or previous_bar.close is None
        ):
            return None
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_bar.close),
                abs(bar.low - previous_bar.close),
            )
        )

    atr = sum(true_ranges[:period]) / period
    for true_range in true_ranges[period:]:
        # Alpha Vantage ATR follows Wilder's smoothing after the initial average.
        atr = ((atr * (period - 1)) + true_range) / period
    return atr, chronological[-1].date


class _PositionPriceProxy:
    """Delegate to a position while overriding ``current_price``.

    Rule evaluation should use the latest cached daily close when one is
    available, but the existing rule helpers read ``position.current_price``.
    This proxy preserves every other attribute from the original position
    via ``__getattr__`` while swapping in the effective price used for the
    current verdict calculation.
    """

    def __init__(self, original_pos: Position, current_price: float):
        self._original_pos = original_pos
        self.current_price = current_price

    def __getattr__(self, name):
        return getattr(self._original_pos, name)


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
        # Guards cache dict mutations so the cache can be safely shared across
        # threads when independent fetches run in parallel.
        self._lock = threading.Lock()

    def get_daily_bars(self, ticker: str) -> list[DailyBar]:
        key = ticker.upper()
        with self._lock:
            if key in self._daily_bars:
                return self._daily_bars[key]
        bars = self._provider.fetch_daily_bars(ticker)
        with self._lock:
            self._daily_bars.setdefault(key, bars)
            return self._daily_bars[key]

    def preload_daily_bars(self, tickers: set[str]) -> None:
        self._preload_bars(tickers, "fetch_daily_bars_batch", self._daily_bars)

    def get_weekly_bars(self, ticker: str) -> list[WeeklyBar]:
        key = ticker.upper()
        with self._lock:
            if key in self._weekly_bars:
                return self._weekly_bars[key]
        bars = self._provider.fetch_weekly_bars(ticker)
        with self._lock:
            self._weekly_bars.setdefault(key, bars)
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
        key = (ticker.upper(), interval, time_period)
        with self._lock:
            if key in self._atr:
                return self._atr[key]
        points = self._provider.fetch_atr(
            ticker, interval=interval, time_period=time_period,
        )
        with self._lock:
            self._atr.setdefault(key, points)
            return self._atr[key]

    def preload_atr(
        self, tickers: set[str], interval: str, time_period: int,
    ) -> None:
        """Batch-fetch ATR for many tickers in a single request when supported.

        Populates the per-operation cache so subsequent ``get_atr`` calls for
        these tickers avoid one rate-limited API call each.
        """
        missing = sorted(
            ticker
            for ticker in tickers
            if (ticker, interval, time_period) not in self._atr
        )
        if not missing:
            return
        if not getattr(type(self._provider), "supports_batch_fetch", False):
            return
        batch_method = getattr(self._provider, "fetch_atr_batch", None)
        if not callable(batch_method):
            return
        try:
            fetched = batch_method(missing, interval, time_period)
        except Exception:
            logger.exception("Batch ATR preload failed for %s ATR-%d", interval, time_period)
            return
        for ticker, points in fetched.items():
            self._atr[(ticker.upper(), interval, time_period)] = points

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

    def compute_atr(
        self,
        ticker: str,
        interval: str,
        period: int,
        as_of: Optional[date_type] = None,
    ) -> Optional[tuple[float, date_type]]:
        """Compute ATR from cached OHLC bars, avoiding an API call."""
        if interval == "daily":
            return _compute_atr(self.get_daily_bars(ticker), period, as_of=as_of)
        if interval == "weekly":
            return _compute_atr(self.get_weekly_bars(ticker), period, as_of=as_of)
        return None


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

    def _daily_bars_for_position(
        self, position: Position, all_daily_bars: dict[str, list]
    ) -> dict[str, list] | None:
        """Return only the daily bar history relevant to one position."""
        relevant: dict[str, list] = {}
        pos_bars = all_daily_bars.get(position.ticker)
        if pos_bars:
            relevant[position.ticker.upper()] = pos_bars
        benchmark = getattr(position, "sector_benchmark_ticker", None)
        if benchmark:
            bench_bars = all_daily_bars.get(benchmark.upper())
            if bench_bars:
                relevant[benchmark.upper()] = bench_bars
        return relevant or None

    def _calculate_verdicts(
        self,
        db: Session,
        positions: list[Position],
        enabled_rules_by_user: dict[str, dict[str, list]] | None = None,
    ) -> dict[int, str]:
        """Return current verdict strings for the supplied positions.

        Verdicts are evaluated against the same cached market-data inputs that
        power the portfolio dashboard, including enabled per-user rule
        selections and any required benchmark history.

        When *enabled_rules_by_user* is provided, the per-user rule lookup is
        skipped — callers that already loaded rule selections (e.g. the
        single-position refresh path) can pass them through to avoid
        re-querying ``StrategyRuleConfig`` and re-running
        ``ensure_strategy_rule_defaults``.
        """
        if not positions:
            return {}

        all_tickers = {position.ticker for position in positions}
        benchmark_tickers = {
            position.sector_benchmark_ticker.upper()
            for position in positions
            if getattr(position, "sector_benchmark_ticker", None)
        }
        with time_block("verdicts.load_indicator_cache"):
            indicator_cache = self.load_indicator_cache_for_tickers(db, all_tickers)
        with time_block("verdicts.load_atr_cache"):
            atr_cache = self.load_atr_cache_for_tickers(db, all_tickers)
        with time_block("verdicts.load_weekly_bars"):
            weekly_bars = self.load_weekly_bar_cache_for_tickers(db, all_tickers)
        with time_block("verdicts.load_daily_bars"):
            daily_bars = self.load_daily_bar_cache_for_tickers(
                db, all_tickers | benchmark_tickers
            )
        if enabled_rules_by_user is None:
            with time_block("verdicts.get_enabled_rules"):
                enabled_rules_by_user = {
                    user_id: rule_config.get_enabled_rule_selections_by_investment_type(
                        as_uow(db, user_id=user_id),
                        user_id=user_id,
                    )
                    for user_id in {position.user_id for position in positions if position.user_id}
                }

        verdicts: dict[int, str] = {}
        for position in positions:
            signals = MarketSignals(
                daily_close=position.daily_close,
                daily_sma_21=position.daily_sma_21,
                weekly_close=position.weekly_close,
                weekly_sma_20=position.weekly_sma_20,
            )

            cached_indicators = indicator_cache.get(position.ticker)
            if cached_indicators:
                signals.ma_signals = dict(cached_indicators)

            cached_atr = atr_cache.get(position.ticker)
            if cached_atr:
                signals.atr_signals = dict(cached_atr)

            cached_weekly_bars = weekly_bars.get(position.ticker)
            if cached_weekly_bars:
                signals.weekly_ohlc_history = [
                    WeeklyOhlcBar(
                        bar_date=bar.bar_date,
                        open=bar.open,
                        high=bar.high,
                        low=bar.low,
                        close=bar.close,
                        volume=bar.volume,
                    )
                    for bar in cached_weekly_bars
                ]

            relevant_daily_bars = self._daily_bars_for_position(position, daily_bars)
            if relevant_daily_bars:
                signals.daily_close_history = {
                    ticker.upper(): [
                        DailyClosePoint(bar_date=bar.bar_date, close=bar.close)
                        for bar in bars
                    ]
                    for ticker, bars in relevant_daily_bars.items()
                }

            effective_price = (
                position.daily_close
                if position.daily_close is not None
                else position.current_price
            )
            configured_rules = enabled_rules_by_user.get(position.user_id, {}).get(
                position.investment_type
            )
            verdicts[id(position)] = get_verdict(
                evaluate_position(
                    _PositionPriceProxy(position, effective_price),
                    signals=signals,
                    configured_rules=configured_rules,
                )
            ).value

        return verdicts

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
        atr_val: Optional[float] = None
        atr_date = None

        if fetch_cache:
            as_of = last_completed_trading_week_end() if interval == "weekly" else None
            computed = fetch_cache.compute_atr(
                ticker, interval, time_period, as_of=as_of,
            )
            if computed is not None:
                atr_val, atr_date = computed

        if atr_val is None:
            points = (
                fetch_cache.get_atr(ticker, interval, time_period)
                if fetch_cache
                else self._provider.fetch_atr(
                    ticker, interval=interval, time_period=time_period,
                )
            )
        else:
            points = []

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

        if fetch_cache:
            for interval, time_period in sorted(required_atr_indicators):
                fallback_tickers: set[str] = set()
                as_of = last_completed_trading_week_end() if interval == "weekly" else None
                for ticker in sorted(tickers):
                    if not force:
                        existing = self._atr_repo.get(
                            db, ticker, interval, time_period,
                        )
                        if not atr_cache_is_stale(existing, interval):
                            continue
                    try:
                        computed = fetch_cache.compute_atr(
                            ticker, interval, time_period, as_of=as_of,
                        )
                    except Exception:
                        logger.debug(
                            "Skipping ATR batch preload check for %s %s ATR-%d",
                            ticker, interval, time_period,
                            exc_info=True,
                        )
                        continue
                    if computed is None:
                        fallback_tickers.add(ticker)
                fetch_cache.preload_atr(fallback_tickers, interval, time_period)

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

        position.daily_retrieved_at = datetime.now(timezone.utc)

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

        position.weekly_retrieved_at = datetime.now(timezone.utc)

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
        started_at = time.monotonic()
        phase_timings = {
            "rule_config": 0.0,
            "prewarm": 0.0,
            "daily_refresh": 0.0,
            "weekly_refresh": 0.0,
            "indicator_cache": 0.0,
            "atr_cache": 0.0,
            "weekly_bar_cache": 0.0,
            "daily_bar_cache": 0.0,
            "verdicts": 0.0,
            "commit": 0.0,
        }
        cache = _FetchCache(self._provider)
        should_refresh_daily = force or daily_data_is_stale(position)
        should_refresh_weekly = self._needs_weekly(position) and (
            force or weekly_data_is_stale(position)
        )
        phase_started_at = time.monotonic()
        rule_uow = as_uow(db, user_id=position.user_id)
        with time_block("rule_config.ensure_defaults"):
            rule_config.ensure_strategy_rule_defaults(rule_uow, user_id=rule_uow.user_id)
        investment_type = position.investment_type
        # Fetch all enabled rule rows once and derive both requirements and
        # selections in Python — avoids two separate DB round-trips.
        with time_block("rule_config.get_rules_and_requirements"):
            requirements, enabled_rules = (
                rule_config.get_rule_requirements_and_selections(
                    rule_uow,
                    user_id=position.user_id,
                    investment_type=investment_type,
                    _skip_defaults=True,
                )
            )
            enabled_rules_by_user = {position.user_id: enabled_rules} if position.user_id else {}
        required = requirements.indicators
        required_atr = requirements.atr_indicators
        weekly_lookback = requirements.weekly_bar_lookback
        daily_lookback = requirements.daily_bar_lookback
        benchmark = getattr(position, "sector_benchmark_ticker", None)
        # Capture the persisted computed_verdict BEFORE any data refresh so we
        # can detect changes and update previous_verdict without a pre-pass
        # _calculate_verdicts call.
        old_computed_verdict = position.computed_verdict
        phase_timings["rule_config"] = time.monotonic() - phase_started_at

        # Warm the per-operation fetch cache by firing independent API calls
        # (daily, weekly, benchmark-daily) concurrently when the provider
        # supports it. Subsequent sequential steps then reuse the cached bars
        # instead of paying the rate-limit gate once per call in series.
        need_daily = (
            should_refresh_daily
            or daily_lookback > 0
            or self._has_interval(required, "daily")
            or self._has_interval(required_atr, "daily")
        )
        need_weekly = (
            should_refresh_weekly
            or weekly_lookback > 0
            or self._has_interval(required, "weekly")
            or self._has_interval(required_atr, "weekly")
        )
        need_benchmark_daily = daily_lookback > 0 and bool(benchmark)
        phase_started_at = time.monotonic()
        self._prewarm_fetch_cache(
            cache,
            position.ticker,
            need_daily=need_daily,
            need_weekly=need_weekly,
            benchmark=benchmark.upper() if benchmark else None,
            need_benchmark_daily=need_benchmark_daily,
        )
        phase_timings["prewarm"] = time.monotonic() - phase_started_at

        # Daily refresh
        phase_started_at = time.monotonic()
        if should_refresh_daily:
            logger.debug("%s daily data is stale, refreshing", position.ticker)
            try:
                self._refresh_daily(position, fetch_cache=cache)
            except Exception as exc:
                logger.exception("Daily refresh failed for %s", position.ticker)
                errors.append(f"Daily refresh failed: {exc}")
        else:
            logger.debug("%s daily data is fresh, skipping", position.ticker)
        phase_timings["daily_refresh"] = time.monotonic() - phase_started_at

        # Weekly refresh (long-term only)
        phase_started_at = time.monotonic()
        if self._needs_weekly(position):
            if should_refresh_weekly:
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
        phase_timings["weekly_refresh"] = time.monotonic() - phase_started_at

        # Refresh indicator caches for configured rules
        phase_started_at = time.monotonic()
        if required:
            cache_errors = self.refresh_indicator_cache(
                db, {position.ticker}, required, force=force,
                fetch_cache=cache,
            )
            errors.extend(cache_errors)
        phase_timings["indicator_cache"] = time.monotonic() - phase_started_at

        phase_started_at = time.monotonic()
        if required_atr:
            atr_errors = self.refresh_atr_cache(
                db, {position.ticker}, required_atr, force=force,
                fetch_cache=cache,
            )
            errors.extend(atr_errors)
        phase_timings["atr_cache"] = time.monotonic() - phase_started_at

        phase_started_at = time.monotonic()
        if weekly_lookback > 0:
            weekly_bar_errors = self.refresh_weekly_bar_cache(
                db, {position.ticker}, weekly_lookback, force=force,
                fetch_cache=cache,
            )
            errors.extend(weekly_bar_errors)
        phase_timings["weekly_bar_cache"] = time.monotonic() - phase_started_at

        phase_started_at = time.monotonic()
        if daily_lookback > 0:
            if benchmark:
                daily_tickers: set[str] = {position.ticker, benchmark.upper()}
                daily_bar_errors = self.refresh_daily_bar_cache(
                    db, daily_tickers, daily_lookback, force=force,
                    fetch_cache=cache,
                )
                errors.extend(daily_bar_errors)
        phase_timings["daily_bar_cache"] = time.monotonic() - phase_started_at

        position.refresh_error = "; ".join(errors) if errors else None
        phase_started_at = time.monotonic()
        with time_block("verdicts.compute"):
            new_verdicts = self._calculate_verdicts(
                db, [position], enabled_rules_by_user=enabled_rules_by_user
            )
            new_computed_verdict = new_verdicts.get(id(position))
            position.computed_verdict = new_computed_verdict
            # Store the prior verdict only when it actually changed so the UI
            # doesn't keep showing stale "Previously ..." text.
            position.previous_verdict = (
                old_computed_verdict
                if old_computed_verdict and old_computed_verdict != new_computed_verdict
                else None
            )
        phase_timings["verdicts"] = time.monotonic() - phase_started_at
        # Mark refresh complete inside the same commit so the background task
        # does not need a separate write (Step 4).
        position.refresh_in_progress = False
        position.refresh_started_at = None
        phase_started_at = time.monotonic()
        with time_block("commit"):
            db.commit()
        phase_timings["commit"] = time.monotonic() - phase_started_at
        logger.info(
            (
                "refresh_position completed for %s in %.3fs "
                "(errors=%d, phases: rule_config=%.3fs prewarm=%.3fs "
                "daily_refresh=%.3fs weekly_refresh=%.3fs indicator_cache=%.3fs "
                "atr_cache=%.3fs weekly_bar_cache=%.3fs daily_bar_cache=%.3fs "
                "verdicts=%.3fs commit=%.3fs)"
            ),
            position.ticker,
            time.monotonic() - started_at,
            len(errors),
            phase_timings["rule_config"],
            phase_timings["prewarm"],
            phase_timings["daily_refresh"],
            phase_timings["weekly_refresh"],
            phase_timings["indicator_cache"],
            phase_timings["atr_cache"],
            phase_timings["weekly_bar_cache"],
            phase_timings["daily_bar_cache"],
            phase_timings["verdicts"],
            phase_timings["commit"],
        )

    @staticmethod
    def _has_interval(indicators: set[tuple[str, int]], interval: str) -> bool:
        """Return True if any required indicator targets *interval*."""
        return any(iv == interval for iv, _ in indicators)

    def _prewarm_fetch_cache(
        self,
        cache: "_FetchCache",
        ticker: str,
        *,
        need_daily: bool,
        need_weekly: bool,
        benchmark: Optional[str],
        need_benchmark_daily: bool,
    ) -> None:
        """Fire independent bar fetches concurrently to warm *cache*.

        Only runs when the provider advertises ``supports_parallel_fetch``.
        Each task populates the thread-safe fetch cache so subsequent
        sequential steps reuse the data without extra API calls. Errors are
        intentionally swallowed here: the sequential steps re-fetch on a cache
        miss and surface failures with proper per-step context.
        """
        if not getattr(type(self._provider), "supports_parallel_fetch", False):
            return

        tasks: list[Callable[[], object]] = []
        if need_daily:
            tasks.append(lambda t=ticker: cache.get_daily_bars(t))
        if need_weekly:
            tasks.append(lambda t=ticker: cache.get_weekly_bars(t))
        if need_benchmark_daily and benchmark:
            # Skip the benchmark-daily fetch when it targets the same symbol as
            # the position's daily fetch. ``get_daily_bars`` fetches outside the
            # cache lock, so enqueuing both would let concurrent misses trigger
            # a duplicate provider call (and burn an extra credit) for one symbol.
            benchmark_duplicates_daily = (
                need_daily and benchmark.upper() == ticker.upper()
            )
            if not benchmark_duplicates_daily:
                tasks.append(lambda b=benchmark: cache.get_daily_bars(b))

        # Nothing to gain from a thread pool for a single fetch.
        if len(tasks) < 2:
            return

        prewarm_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = [executor.submit(task) for task in tasks]
            for future in futures:
                try:
                    future.result()
                except Exception:
                    logger.debug(
                        "Parallel prewarm fetch failed for %s; "
                        "will retry sequentially",
                        ticker,
                        exc_info=True,
                    )
        logger.debug(
            "Parallel prewarm of %d fetches for %s took %.3fs",
            len(tasks),
            ticker,
            time.monotonic() - prewarm_start,
        )

    def refresh_all_positions(
        self,
        db: Session,
        force: bool = False,
        user_id: str | None = None,
        user_ids: set[str] | None = None,
    ) -> int:
        """Refresh cached market data for all positions.

        Deduplicates API calls: positions sharing the same ticker are grouped
        so that each unique ticker is fetched at most once.

        Also refreshes indicator / ATR / bar caches for configured rules.

        Pass ``user_id`` to scope the refresh to a single user (the
        user-facing /refresh route). Pass ``user_ids`` to scope to a set of
        users (the nightly job uses this to batch all full_access users in
        one fetch pass so duplicate tickers across users share API calls).
        ``user_id`` and ``user_ids`` are mutually exclusive.

        Returns the number of positions that were actually refreshed.
        """
        if user_id is not None and user_ids is not None:
            raise ValueError("Pass either user_id or user_ids, not both")

        q = db.query(Position)
        if user_id is not None:
            q = q.filter(Position.user_id == user_id)
        elif user_ids is not None:
            if not user_ids:
                logger.info("Refresh skipped: empty user_ids filter")
                return 0
            q = q.filter(Position.user_id.in_(user_ids))
        positions = q.all()
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

        user_ids = {user_id} if user_id is not None else {pos.user_id for pos in positions}
        required: set[tuple[str, int]] = set()
        required_atr: set[tuple[str, int]] = set()
        weekly_lookback = 0
        daily_lookback = 0
        # Load enabled rule selections once per user — reuse for the two
        # _calculate_verdicts passes below to avoid duplicate
        # ensure_defaults / get_enabled_rules queries.
        enabled_rules_by_user: dict[str, dict[str, list]] = {}
        for current_user_id in user_ids:
            rule_uow = as_uow(db, user_id=current_user_id)
            rule_config.ensure_strategy_rule_defaults(rule_uow, user_id=current_user_id)
            requirements = rule_config.get_rule_requirements(rule_uow, _skip_defaults=True)
            required.update(requirements.indicators)
            required_atr.update(requirements.atr_indicators)
            weekly_lookback = max(weekly_lookback, requirements.weekly_bar_lookback)
            daily_lookback = max(daily_lookback, requirements.daily_bar_lookback)
            if current_user_id:
                enabled_rules_by_user[current_user_id] = (
                    rule_config.get_enabled_rule_selections_by_investment_type(
                        rule_uow, user_id=current_user_id, _skip_defaults=True
                    )
                )

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

        all_tickers = set(ticker_groups.keys())
        for interval, _time_period in required:
            if interval == "daily":
                daily_batch_tickers.update(all_tickers)
            elif interval == "weekly":
                weekly_batch_tickers.update(all_tickers)

        for interval, _time_period in required_atr:
            if interval == "daily":
                daily_batch_tickers.update(all_tickers)
            elif interval == "weekly":
                weekly_batch_tickers.update(all_tickers)

        if weekly_lookback > 0:
            weekly_batch_tickers.update(all_tickers)

        daily_rule_tickers: set[str] = set()
        if daily_lookback > 0:
            for pos in positions:
                benchmark = getattr(pos, "sector_benchmark_ticker", None)
                if benchmark:
                    daily_rule_tickers.add(pos.ticker.upper())
                    daily_rule_tickers.add(benchmark.upper())
            daily_batch_tickers.update(daily_rule_tickers)

        cache.preload_daily_bars(daily_batch_tickers)
        cache.preload_weekly_bars(weekly_batch_tickers)
        rule_inputs_may_refresh = bool(
            required or required_atr or weekly_lookback > 0 or daily_rule_tickers
        )
        tracked_positions = [
            pos
            for ticker, group in ticker_groups.items()
            if rule_inputs_may_refresh
            or refresh_plan[ticker]["group_needs_daily"]
            or refresh_plan[ticker]["group_needs_weekly"]
            for pos in group
        ]
        # Snapshot persisted computed_verdict before any data changes so we can
        # detect verdict flips without a pre-pass _calculate_verdicts call.
        old_computed_verdicts = {id(pos): pos.computed_verdict for pos in tracked_positions}

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

            completed_group_ids = {id(pos) for pos in group}
            heartbeat = datetime.now(timezone.utc)
            for pos in positions:
                if not getattr(pos, "refresh_in_progress", False):
                    continue
                if id(pos) in completed_group_ids:
                    pos.refresh_in_progress = False
                    pos.refresh_started_at = None
                else:
                    pos.refresh_started_at = heartbeat

            db.commit()

        if required and ticker_groups:
            cache_errors = self.refresh_indicator_cache(
                db, all_tickers, required, force=force,
                fetch_cache=cache,
            )
            if cache_errors:
                logger.warning("Indicator cache refresh errors: %s", cache_errors)

        if required_atr and ticker_groups:
            atr_errors = self.refresh_atr_cache(
                db, all_tickers, required_atr, force=force,
                fetch_cache=cache,
            )
            if atr_errors:
                logger.warning("ATR cache refresh errors: %s", atr_errors)

        if weekly_lookback > 0 and ticker_groups:
            bar_errors = self.refresh_weekly_bar_cache(
                db, all_tickers, weekly_lookback, force=force,
                fetch_cache=cache,
            )
            if bar_errors:
                logger.warning("Weekly bar cache refresh errors: %s", bar_errors)

        if daily_lookback > 0 and ticker_groups:
            if daily_rule_tickers:
                daily_errors = self.refresh_daily_bar_cache(
                    db, daily_rule_tickers, daily_lookback, force=force,
                    fetch_cache=cache,
                )
                if daily_errors:
                    logger.warning("Daily bar cache refresh errors: %s", daily_errors)

        if tracked_positions:
            new_verdicts = self._calculate_verdicts(
                db, tracked_positions, enabled_rules_by_user=enabled_rules_by_user
            )
            for pos in tracked_positions:
                new_cv = new_verdicts.get(id(pos))
                pos.computed_verdict = new_cv
                old_cv = old_computed_verdicts.get(id(pos))
                pos.previous_verdict = (
                    old_cv if old_cv and old_cv != new_cv else None
                )
            db.commit()

        logger.info(
            "Refresh complete: %d/%d positions refreshed",
            refreshed, len(positions),
        )
        return refreshed
