"""Market data provider abstraction.

Defines the ``MarketDataProvider`` protocol and the
``AlphaVantageProvider`` implementation.  The provider owns rate limiting
and API-key resolution so that callers never need to manage those concerns.
"""

import logging
import threading
import time
from collections import deque
from typing import Callable, Optional, Protocol, TypeVar, runtime_checkable

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
_T = TypeVar("_T")


def _join_symbols_for_logging(symbols: list[str]) -> str:
    return ",".join(symbols)


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
    def _wait_for_slot(cls) -> float:
        # Reserve the next slot under the lock, then release it before
        # sleeping so the lock is never held during ``time.sleep``.
        min_interval_seconds = get_alpha_vantage_min_interval_seconds()
        with cls._lock:
            now = time.monotonic()
            if cls._last_call_at is None:
                reserved = now
            else:
                reserved = max(now, cls._last_call_at + min_interval_seconds)
            cls._last_call_at = reserved
        wait_for = reserved - time.monotonic()
        if wait_for > 0:
            logger.info(
                "Rate-limit: sleeping %.1fs before next API call",
                wait_for,
            )
            time.sleep(wait_for)
        return max(wait_for, 0.0)

    def _rate_limited_call(
        self, endpoint: str, target: str, fetcher: Callable[[], _T],
    ) -> _T:
        waited = self._wait_for_slot()
        started_at = time.monotonic()
        result = fetcher()
        logger.info(
            "AlphaVantageProvider %s %s: waited=%.2fs fetch=%.2fs",
            endpoint,
            target,
            waited,
            time.monotonic() - started_at,
        )
        return result

    # -- protocol methods ---------------------------------------------------

    def fetch_company_name(self, symbol: str) -> str:
        return self._rate_limited_call(
            "fetch_company_name",
            symbol,
            lambda: _av_fetch_company_name(symbol, self._get_api_key()),
        )

    def fetch_ticker_matches(self, symbol: str) -> list[SymbolSearchMatch]:
        return self._rate_limited_call(
            "fetch_ticker_matches",
            symbol,
            lambda: _av_fetch_ticker_matches(symbol, self._get_api_key()),
        )

    def fetch_daily_bars(self, symbol: str) -> list[DailyBar]:
        return self._rate_limited_call(
            "fetch_daily_bars",
            symbol,
            lambda: _av_fetch_daily_series(symbol, self._get_api_key()),
        )

    def fetch_weekly_bars(self, symbol: str) -> list[WeeklyBar]:
        return self._rate_limited_call(
            "fetch_weekly_bars",
            symbol,
            lambda: _av_fetch_weekly_series(symbol, self._get_api_key()),
        )

    def fetch_sma(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[SMAPoint]:
        return self._rate_limited_call(
            f"fetch_sma[{interval}:{time_period}]",
            symbol,
            lambda: _av_fetch_sma(
                symbol, interval=interval, time_period=time_period,
                api_key=self._get_api_key(),
            ),
        )

    def fetch_atr(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[ATRPoint]:
        return self._rate_limited_call(
            f"fetch_atr[{interval}:{time_period}]",
            symbol,
            lambda: _av_fetch_atr(
                symbol, interval=interval, time_period=time_period,
                api_key=self._get_api_key(),
            ),
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
    def _wait_for_slot(cls) -> float:
        """Block until it is safe to make another API call.

        When ``TWELVE_DATA_CREDITS_PER_MINUTE`` is configured, a rolling
        60-second credit-budget gate is used: concurrent calls proceed
        without delay as long as fewer than the budgeted number of calls
        occurred in the trailing minute. Otherwise the legacy strict
        per-call interval gate is used for backwards compatibility.
        """
        credits_per_minute = get_twelve_data_credits_per_minute()
        if credits_per_minute is None:
            return cls._wait_for_interval_slot()
        return cls._wait_for_budget_slot(credits_per_minute)

    @classmethod
    def _wait_for_interval_slot(cls) -> float:
        # Reserve the next evenly-spaced slot atomically under the lock, then
        # release the lock before sleeping. Holding the lock across the sleep
        # would serialize concurrent prewarm threads; reserving the slot first
        # lets their waits overlap while still spacing calls by the configured
        # interval on average.
        min_interval_seconds = get_twelve_data_min_interval_seconds()
        with cls._lock:
            now = time.monotonic()
            if cls._last_call_at is None:
                reserved = now
            else:
                reserved = max(now, cls._last_call_at + min_interval_seconds)
            cls._last_call_at = reserved
        wait_for = reserved - time.monotonic()
        if wait_for > 0:
            logger.info(
                "Rate-limit: sleeping %.1fs before next API call",
                wait_for,
            )
            time.sleep(wait_for)
        return max(wait_for, 0.0)

    @classmethod
    def _wait_for_budget_slot(cls, credits_per_minute: int) -> float:
        # Reserve a slot in the rolling window under the lock, then sleep
        # outside it. When the budget is exhausted, space the new slot 60s
        # after the reservation ``credits_per_minute`` entries back, rather
        # than after the single oldest call. Reserving relative to prior
        # reservations stops concurrent callers from all claiming the same
        # ``oldest + 60`` slot and bursting past the cap at the window edge.
        with cls._lock:
            now = time.monotonic()
            cls._prune_call_window(now)
            window_depth = len(cls._call_window)
            if window_depth >= credits_per_minute:
                reserved = max(
                    now,
                    cls._call_window[-credits_per_minute] + cls._WINDOW_SECONDS,
                )
            else:
                reserved = now
            cls._call_window.append(reserved)
        wait_for = reserved - time.monotonic()
        logger.info(
            "TwelveDataProvider rate_limit: window=%d/%d (%s)",
            window_depth,
            credits_per_minute,
            (
                f"sleeping {wait_for:.1f}s"
                if wait_for > 0
                else "no wait"
            ),
        )
        if wait_for > 0:
            time.sleep(wait_for)
        return max(wait_for, 0.0)

    @classmethod
    def _prune_call_window(cls, now: float) -> None:
        window_start = now - cls._WINDOW_SECONDS
        while cls._call_window and cls._call_window[0] <= window_start:
            cls._call_window.popleft()

    def _rate_limited_call(
        self, endpoint: str, target: str, fetcher: Callable[[], _T],
    ) -> _T:
        waited = self._wait_for_slot()
        started_at = time.monotonic()
        result = fetcher()
        logger.info(
            "TwelveDataProvider %s %s: waited=%.2fs fetch=%.2fs",
            endpoint,
            target,
            waited,
            time.monotonic() - started_at,
        )
        return result

    def fetch_company_name(self, symbol: str) -> str:
        return self._rate_limited_call(
            "fetch_company_name",
            symbol,
            lambda: _td_fetch_company_name(symbol, self._get_api_key()),
        )

    def fetch_ticker_matches(self, symbol: str) -> list[SymbolSearchMatch]:
        return self._rate_limited_call(
            "fetch_ticker_matches",
            symbol,
            lambda: _td_fetch_ticker_matches(symbol, self._get_api_key()),
        )

    def fetch_daily_bars(self, symbol: str) -> list[DailyBar]:
        return self._rate_limited_call(
            "fetch_daily_bars",
            symbol,
            lambda: _td_fetch_daily_series(symbol, self._get_api_key()),
        )

    def fetch_weekly_bars(self, symbol: str) -> list[WeeklyBar]:
        return self._rate_limited_call(
            "fetch_weekly_bars",
            symbol,
            lambda: _td_fetch_weekly_series(symbol, self._get_api_key()),
        )

    def fetch_daily_bars_batch(self, symbols: list[str]) -> dict[str, list[DailyBar]]:
        return self._rate_limited_call(
            "fetch_daily_bars_batch",
            _join_symbols_for_logging(symbols),
            lambda: _td_fetch_daily_series_batch(symbols, self._get_api_key()),
        )

    def fetch_weekly_bars_batch(self, symbols: list[str]) -> dict[str, list[WeeklyBar]]:
        return self._rate_limited_call(
            "fetch_weekly_bars_batch",
            _join_symbols_for_logging(symbols),
            lambda: _td_fetch_weekly_series_batch(symbols, self._get_api_key()),
        )

    def fetch_sma(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[SMAPoint]:
        return self._rate_limited_call(
            f"fetch_sma[{interval}:{time_period}]",
            symbol,
            lambda: _td_fetch_sma(
                symbol, interval=interval, time_period=time_period,
                api_key=self._get_api_key(),
            ),
        )

    def fetch_atr(
        self, symbol: str, interval: str, time_period: int,
    ) -> list[ATRPoint]:
        return self._rate_limited_call(
            f"fetch_atr[{interval}:{time_period}]",
            symbol,
            lambda: _td_fetch_atr(
                symbol, interval=interval, time_period=time_period,
                api_key=self._get_api_key(),
            ),
        )

    def fetch_atr_batch(
        self, symbols: list[str], interval: str, time_period: int,
    ) -> dict[str, list[ATRPoint]]:
        return self._rate_limited_call(
            f"fetch_atr_batch[{interval}:{time_period}]",
            _join_symbols_for_logging(symbols),
            lambda: _td_fetch_atr_batch(
                symbols, interval=interval, time_period=time_period,
                api_key=self._get_api_key(),
            ),
        )
