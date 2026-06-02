"""Market data provider abstraction.

Defines the ``MarketDataProvider`` protocol and the
``AlphaVantageProvider`` implementation.  The provider owns rate limiting
and API-key resolution so that callers never need to manage those concerns.
"""

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional, Protocol, runtime_checkable

from app.alpha_vantage_client import (
    ATRPoint,
    DailyBar,
    SMAPoint,
    SymbolSearchMatch,
    WeeklyBar,
    fetch_atr as _av_fetch_atr,
    fetch_company_name as _av_fetch_company_name,
    fetch_ticker_matches as _av_fetch_ticker_matches,
    fetch_daily_series as _av_fetch_daily_series,
    fetch_sma as _av_fetch_sma,
    fetch_weekly_series as _av_fetch_weekly_series,
)
from app.config import require_alpha_vantage_api_key, require_twelve_data_api_key
from app.config import (
    get_alpha_vantage_min_interval_seconds,
    get_twelve_data_credits_per_minute,
    get_twelve_data_min_interval_seconds,
)
from app.twelve_data_client import (
    fetch_atr as _td_fetch_atr,
    fetch_atr_batch as _td_fetch_atr_batch,
    fetch_company_name as _td_fetch_company_name,
    fetch_ticker_matches as _td_fetch_ticker_matches,
    fetch_daily_series_batch as _td_fetch_daily_series_batch,
    fetch_daily_series as _td_fetch_daily_series,
    fetch_sma as _td_fetch_sma,
    fetch_weekly_series_batch as _td_fetch_weekly_series_batch,
    fetch_weekly_series as _td_fetch_weekly_series,
)

logger = logging.getLogger(__name__)


@runtime_checkable
class MarketDataProvider(Protocol):
    """Contract for any market data source."""

    def fetch_company_name(self, symbol: str) -> str: ...

    def fetch_ticker_matches(self, symbol: str) -> list[SymbolSearchMatch]: ...

    def fetch_daily_bars(self, symbol: str) -> list[DailyBar]: ...

    def fetch_weekly_bars(self, symbol: str) -> list[WeeklyBar]: ...

    def fetch_sma(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[SMAPoint]: ...

    def fetch_atr(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[ATRPoint]: ...


class AlphaVantageProvider:
    """Alpha Vantage implementation of :class:`MarketDataProvider`.

    Rate limiting is shared across all instances via a class-level lock
    and timestamp so that multiple provider objects in the same process
    respect the free-tier throttle.

    The API key is resolved lazily on each call via *get_api_key* so that
    tests can inject a fake without touching environment variables.
    """

    supports_batch_fetch = False
    supports_parallel_fetch = False
    _last_call_at: Optional[float] = None
    _lock = threading.Lock()

    def __init__(
        self,
        get_api_key: Callable[[], str] = require_alpha_vantage_api_key,
    ) -> None:
        self._get_api_key = get_api_key

    # -- rate limiting (class-level, shared across instances) ----------------

    @classmethod
    def _wait_for_slot(cls) -> None:
        with cls._lock:
            min_interval_seconds = get_alpha_vantage_min_interval_seconds()
            now = time.monotonic()
            if cls._last_call_at is not None:
                elapsed = now - cls._last_call_at
                remaining = min_interval_seconds - elapsed
                if remaining > 0:
                    logger.debug(
                        "Rate-limit: sleeping %.1fs before next API call",
                        remaining,
                    )
                    time.sleep(remaining)
            cls._last_call_at = time.monotonic()

    # -- protocol methods ---------------------------------------------------

    def fetch_company_name(self, symbol: str) -> str:
        self._wait_for_slot()
        return _av_fetch_company_name(symbol, self._get_api_key())

    def fetch_ticker_matches(self, symbol: str) -> list[SymbolSearchMatch]:
        self._wait_for_slot()
        return _av_fetch_ticker_matches(symbol, self._get_api_key())

    def fetch_daily_bars(self, symbol: str) -> list[DailyBar]:
        self._wait_for_slot()
        return _av_fetch_daily_series(symbol, self._get_api_key())

    def fetch_weekly_bars(self, symbol: str) -> list[WeeklyBar]:
        self._wait_for_slot()
        return _av_fetch_weekly_series(symbol, self._get_api_key())

    def fetch_sma(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[SMAPoint]:
        self._wait_for_slot()
        return _av_fetch_sma(
            symbol, interval=interval, time_period=time_period,
            api_key=self._get_api_key(),
        )

    def fetch_atr(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[ATRPoint]:
        self._wait_for_slot()
        return _av_fetch_atr(
            symbol, interval=interval, time_period=time_period,
            api_key=self._get_api_key(),
        )


class TwelveDataProvider:
    """Twelve Data implementation of :class:`MarketDataProvider`."""

    supports_batch_fetch = True
    supports_parallel_fetch = True
    _WINDOW_SECONDS = 60.0
    _last_call_at: Optional[float] = None
    _lock = threading.Lock()
    # Monotonic timestamps of recent calls, used by the credit-budget gate.
    _call_window: "deque[float]" = deque()

    def __init__(
        self,
        get_api_key: Callable[[], str] = require_twelve_data_api_key,
    ) -> None:
        self._get_api_key = get_api_key

    @classmethod
    def _wait_for_slot(cls) -> None:
        """Block until it is safe to make another API call.

        When ``TWELVE_DATA_CREDITS_PER_MINUTE`` is configured, a rolling
        60-second credit-budget gate is used: concurrent calls proceed
        without delay as long as fewer than the budgeted number of calls
        occurred in the trailing minute. Otherwise the legacy strict
        per-call interval gate is used for backwards compatibility.
        """
        credits_per_minute = get_twelve_data_credits_per_minute()
        if credits_per_minute is None:
            cls._wait_for_interval_slot()
            return
        cls._wait_for_budget_slot(credits_per_minute)

    @classmethod
    def _wait_for_interval_slot(cls) -> None:
        with cls._lock:
            min_interval_seconds = get_twelve_data_min_interval_seconds()
            now = time.monotonic()
            if cls._last_call_at is not None:
                elapsed = now - cls._last_call_at
                remaining = min_interval_seconds - elapsed
                if remaining > 0:
                    logger.debug(
                        "Rate-limit: sleeping %.1fs before next API call",
                        remaining,
                    )
                    time.sleep(remaining)
            cls._last_call_at = time.monotonic()

    @classmethod
    def _wait_for_budget_slot(cls, credits_per_minute: int) -> None:
        with cls._lock:
            now = time.monotonic()
            cls._prune_call_window(now)
            if len(cls._call_window) >= credits_per_minute:
                # Budget exhausted: wait until the oldest call leaves the
                # trailing 60s window, then re-prune.
                wait_for = cls._call_window[0] + cls._WINDOW_SECONDS - now
                if wait_for > 0:
                    logger.debug(
                        "Rate-limit budget exhausted: sleeping %.1fs",
                        wait_for,
                    )
                    time.sleep(wait_for)
                cls._prune_call_window(time.monotonic())
            cls._call_window.append(time.monotonic())

    @classmethod
    def _prune_call_window(cls, now: float) -> None:
        window_start = now - cls._WINDOW_SECONDS
        while cls._call_window and cls._call_window[0] <= window_start:
            cls._call_window.popleft()

    def fetch_company_name(self, symbol: str) -> str:
        self._wait_for_slot()
        return _td_fetch_company_name(symbol, self._get_api_key())

    def fetch_ticker_matches(self, symbol: str) -> list[SymbolSearchMatch]:
        self._wait_for_slot()
        return _td_fetch_ticker_matches(symbol, self._get_api_key())

    def fetch_daily_bars(self, symbol: str) -> list[DailyBar]:
        self._wait_for_slot()
        return _td_fetch_daily_series(symbol, self._get_api_key())

    def fetch_weekly_bars(self, symbol: str) -> list[WeeklyBar]:
        self._wait_for_slot()
        return _td_fetch_weekly_series(symbol, self._get_api_key())

    def fetch_daily_bars_batch(self, symbols: list[str]) -> dict[str, list[DailyBar]]:
        self._wait_for_slot()
        return _td_fetch_daily_series_batch(symbols, self._get_api_key())

    def fetch_weekly_bars_batch(self, symbols: list[str]) -> dict[str, list[WeeklyBar]]:
        self._wait_for_slot()
        return _td_fetch_weekly_series_batch(symbols, self._get_api_key())

    def fetch_sma(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[SMAPoint]:
        self._wait_for_slot()
        return _td_fetch_sma(
            symbol, interval=interval, time_period=time_period,
            api_key=self._get_api_key(),
        )

    def fetch_atr(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[ATRPoint]:
        self._wait_for_slot()
        return _td_fetch_atr(
            symbol, interval=interval, time_period=time_period,
            api_key=self._get_api_key(),
        )

    def fetch_atr_batch(
        self, symbols: list[str], interval: str, time_period: int,
    ) -> dict[str, list[ATRPoint]]:
        self._wait_for_slot()
        return _td_fetch_atr_batch(
            symbols, interval=interval, time_period=time_period,
            api_key=self._get_api_key(),
        )
