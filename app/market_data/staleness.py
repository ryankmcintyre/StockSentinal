"""Pure staleness-check functions.

These helpers decide whether cached market data is fresh enough to skip
a re-fetch.  They are pure functions with no I/O and can be tested in
isolation.
"""

from datetime import date, timedelta
from typing import Optional

from app.models import MarketAtrCache, MarketDailyBarCache, MarketIndicatorCache, MarketWeeklyBarCache, Position


def last_completed_trading_day(today: Optional[date] = None) -> date:
    """Return the most recently completed trading day (previous business day).

    Weekends are skipped; holidays are not accounted for (would require a
    calendar data source). The result is always strictly before *today*.
    """
    today = today or date.today()
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:  # 5=Saturday, 6=Sunday
        candidate -= timedelta(days=1)
    return candidate


def last_completed_trading_week_end(today: Optional[date] = None) -> date:
    """Return the Friday that ended the most recently completed trading week.

    If today is Saturday or later in the weekend the completed week is the one
    that just ended on Friday. If today is Mon-Fri the completed week ended
    the *previous* Friday.
    """
    today = today or date.today()
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
    target = last_completed_trading_day(today)
    return position.daily_market_date < target


def weekly_data_is_stale(position: Position, today: Optional[date] = None) -> bool:
    """Return True if the cached weekly snapshot is missing or does not match
    the most recently completed trading week end."""
    if position.weekly_market_date is None:
        return True
    target = last_completed_trading_week_end(today)
    return position.weekly_market_date != target


def indicator_cache_is_stale(
    cache_row: Optional[MarketIndicatorCache],
    interval: str,
    today: Optional[date] = None,
) -> bool:
    """Return True if an indicator cache entry is stale, incomplete, or missing."""
    if (
        cache_row is None
        or cache_row.sma_date is None
        or cache_row.close_date is None
        or cache_row.close_value is None
    ):
        return True
    if interval == "daily":
        target = last_completed_trading_day(today)
        return cache_row.sma_date < target or cache_row.close_date != target
    elif interval == "weekly":
        target = last_completed_trading_week_end(today)
        return cache_row.sma_date != target or cache_row.close_date != target
    return True


def atr_cache_is_stale(
    cache_row: Optional[MarketAtrCache],
    interval: str,
    today: Optional[date] = None,
) -> bool:
    """Return True if an ATR cache entry is stale, incomplete, or missing."""
    if cache_row is None or cache_row.atr_date is None or cache_row.atr_value is None:
        return True
    if interval == "daily":
        target = last_completed_trading_day(today)
        return cache_row.atr_date < target
    elif interval == "weekly":
        target = last_completed_trading_week_end(today)
        return cache_row.atr_date != target
    return True


def weekly_bar_cache_is_stale(
    latest_row: Optional[MarketWeeklyBarCache],
    today: Optional[date] = None,
) -> bool:
    """Return True if the most recent cached weekly bar is older than the
    most recently completed trading week."""
    if latest_row is None or latest_row.bar_date is None:
        return True
    target = last_completed_trading_week_end(today)
    return latest_row.bar_date < target


def daily_bar_cache_is_stale(
    latest_row: Optional[MarketDailyBarCache],
    today: Optional[date] = None,
) -> bool:
    """Return True if the most recent cached daily bar is older than the
    most recently completed trading day."""
    if latest_row is None or latest_row.bar_date is None:
        return True
    target = last_completed_trading_day(today)
    return latest_row.bar_date < target
