"""Market data provider abstraction.

Defines the ``MarketDataProvider`` protocol and the
``AlphaVantageProvider`` implementation.  The provider owns rate limiting
and API-key resolution so that callers never need to manage those concerns.
"""

import logging
import threading
import time
from typing import Callable, Optional, Protocol, runtime_checkable

from app.alpha_vantage_client import (
    ATRPoint,
    DailyBar,
    SMAPoint,
    WeeklyBar,
    fetch_atr as _av_fetch_atr,
    fetch_company_name as _av_fetch_company_name,
    fetch_daily_series as _av_fetch_daily_series,
    fetch_sma as _av_fetch_sma,
    fetch_weekly_series as _av_fetch_weekly_series,
)
from app.config import require_alpha_vantage_api_key

logger = logging.getLogger(__name__)


@runtime_checkable
class MarketDataProvider(Protocol):
    """Contract for any market data source."""

    def fetch_company_name(self, symbol: str) -> str: ...

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

    _MIN_INTERVAL_SECONDS = 12.0
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
            now = time.monotonic()
            if cls._last_call_at is not None:
                elapsed = now - cls._last_call_at
                remaining = cls._MIN_INTERVAL_SECONDS - elapsed
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
