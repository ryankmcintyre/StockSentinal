"""Market data service layer.

Orchestrates Alpha Vantage API calls, applies staleness checks so that
daily data is fetched at most once per completed trading day and weekly
data at most once per completed trading week, and persists the cached
snapshot back to SQLite.
"""

import time
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.alpha_vantage_client import (
    AlphaVantageError,
    fetch_daily_series as _fetch_daily_series,
    fetch_sma as _fetch_sma,
    fetch_weekly_series as _fetch_weekly_series,
)
from app.config import require_alpha_vantage_api_key
from app.models import Position
from app.schemas import InvestmentType


_ALPHA_VANTAGE_MIN_INTERVAL_SECONDS = 12.0
_last_alpha_vantage_call_at: Optional[float] = None


def _wait_for_alpha_vantage_slot() -> None:
    """Enforce the Alpha Vantage free-tier rate limit across all calls."""
    global _last_alpha_vantage_call_at

    now = time.monotonic()
    if _last_alpha_vantage_call_at is not None:
        elapsed = now - _last_alpha_vantage_call_at
        remaining = _ALPHA_VANTAGE_MIN_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    _last_alpha_vantage_call_at = time.monotonic()


def fetch_daily_series(symbol: str, api_key: str):
    """Rate-limited wrapper for Alpha Vantage daily series requests."""
    _wait_for_alpha_vantage_slot()
    return _fetch_daily_series(symbol, api_key)


def fetch_weekly_series(symbol: str, api_key: str):
    """Rate-limited wrapper for Alpha Vantage weekly series requests."""
    _wait_for_alpha_vantage_slot()
    return _fetch_weekly_series(symbol, api_key)


def fetch_sma(symbol: str, interval: str, time_period: int, api_key: str):
    """Rate-limited wrapper for Alpha Vantage SMA requests."""
    _wait_for_alpha_vantage_slot()
    return _fetch_sma(symbol, interval=interval, time_period=time_period, api_key=api_key)


# ---------------------------------------------------------------------------
# Staleness helpers
# ---------------------------------------------------------------------------


def _last_completed_trading_day(today: Optional[date] = None) -> date:
    """Return the most recently completed trading day (previous business day).

    Weekends are skipped; holidays are not accounted for (would require a
    calendar data source). The result is always strictly before *today*.
    """
    today = today or date.today()
    candidate = today - timedelta(days=1)
    # Skip weekends: Saturday -> Friday, Sunday -> Friday
    while candidate.weekday() >= 5:  # 5=Saturday, 6=Sunday
        candidate -= timedelta(days=1)
    return candidate


def _last_completed_trading_week_end(today: Optional[date] = None) -> date:
    """Return the Friday that ended the most recently completed trading week.

    If today is Saturday or later in the weekend the completed week is the one
    that just ended on Friday. If today is Mon-Fri the completed week ended
    the *previous* Friday.
    """
    today = today or date.today()
    # weekday(): Mon=0 … Sun=6
    days_since_friday = (today.weekday() - 4) % 7
    if days_since_friday == 0 and today.weekday() == 4:
        # today IS Friday; the completed week ended last Friday
        days_since_friday = 7
    last_friday = today - timedelta(days=days_since_friday)
    return last_friday


def daily_data_is_stale(position: Position, today: Optional[date] = None) -> bool:
    """Return True if the cached daily snapshot is missing or older than the
    most recently completed trading day."""
    if position.daily_market_date is None:
        return True
    target = _last_completed_trading_day(today)
    return position.daily_market_date < target


def weekly_data_is_stale(position: Position, today: Optional[date] = None) -> bool:
    """Return True if the cached weekly snapshot is missing or does not match
    the most recently completed trading week end."""
    if position.weekly_market_date is None:
        return True
    target = _last_completed_trading_week_end(today)
    return position.weekly_market_date != target


# ---------------------------------------------------------------------------
# Refresh logic
# ---------------------------------------------------------------------------


def _refresh_daily(position: Position, api_key: str) -> None:
    """Fetch and cache daily close + SMA-21 for a position."""
    symbol = position.ticker

    # Fetch daily price series — we only need the most recent bar
    bars = fetch_daily_series(symbol, api_key)
    if not bars:
        raise AlphaVantageError(f"No daily bars returned for {symbol}")

    latest_bar = bars[0]
    position.daily_close = latest_bar.close
    position.daily_market_date = latest_bar.date

    # Fetch SMA-21 daily
    sma_points = fetch_sma(symbol, interval="daily", time_period=21, api_key=api_key)
    if sma_points:
        position.daily_sma_21 = sma_points[0].sma

    position.daily_retrieved_at = datetime.now()


def _refresh_weekly(position: Position, api_key: str) -> None:
    """Fetch and cache weekly close + SMA-20 for a position."""
    symbol = position.ticker

    # Fetch weekly price series
    bars = fetch_weekly_series(symbol, api_key)
    if not bars:
        raise AlphaVantageError(f"No weekly bars returned for {symbol}")

    # The first bar may represent the current (incomplete) week.
    # Use the second bar if the first bar's date is in the current week.
    target_friday = _last_completed_trading_week_end()
    latest_bar = bars[0]
    if latest_bar.date > target_friday and len(bars) > 1:
        latest_bar = bars[1]

    position.weekly_close = latest_bar.close
    position.weekly_market_date = latest_bar.date

    # Rate-limit: Alpha Vantage free tier allows 5 calls/minute
    time.sleep(12)

    # Fetch SMA-20 weekly
    sma_points = fetch_sma(symbol, interval="weekly", time_period=20, api_key=api_key)
    if sma_points:
        # Pick the SMA point that aligns with the bar we chose
        for pt in sma_points:
            if pt.date <= target_friday:
                position.weekly_sma_20 = pt.sma
                break
        else:
            position.weekly_sma_20 = sma_points[0].sma

    position.weekly_retrieved_at = datetime.now()


def refresh_position(position: Position, db: Session, force: bool = False) -> None:
    """Refresh cached market data for a single position.

    Respects staleness checks unless *force* is True. Errors are stored in
    position.refresh_error so the UI can report them without crashing.
    """
    api_key = require_alpha_vantage_api_key()
    errors: list[str] = []

    # Daily refresh (needed by both short-term and long-term positions)
    if force or daily_data_is_stale(position):
        try:
            _refresh_daily(position, api_key)
        except Exception as exc:
            errors.append(f"Daily refresh failed: {exc}")

    # Weekly refresh (needed only by long-term positions, but fetch for all
    # so we have the data if the user changes investment_type later)
    if force or weekly_data_is_stale(position):
        try:
            _refresh_weekly(position, api_key)
        except Exception as exc:
            errors.append(f"Weekly refresh failed: {exc}")

    position.refresh_error = "; ".join(errors) if errors else None
    db.commit()


def refresh_all_positions(db: Session, force: bool = False) -> int:
    """Refresh cached market data for all positions.

    Returns the number of positions that were actually refreshed (i.e. had
    stale data or force=True).
    """
    positions = db.query(Position).all()
    refreshed = 0
    for pos in positions:
        needs_daily = force or daily_data_is_stale(pos)
        needs_weekly = force or weekly_data_is_stale(pos)
        if needs_daily or needs_weekly:
            refresh_position(pos, db, force=force)
            refreshed += 1
    return refreshed
