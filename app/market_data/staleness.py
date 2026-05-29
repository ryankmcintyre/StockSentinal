"""Pure staleness-check functions.

These helpers decide whether cached market data is fresh enough to skip
a re-fetch.  They are pure functions with no I/O and can be tested in
isolation.
"""

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from app.models import MarketAtrCache, MarketDailyBarCache, MarketIndicatorCache, MarketWeeklyBarCache, Position

_ET = ZoneInfo("America/New_York")
_MARKET_CLOSE_HOUR = 16  # 4:00 PM ET


def _et_now() -> datetime:
    """Return the current time in US Eastern."""
    return datetime.now(_ET)


def last_completed_trading_day(
    today: Optional[date] = None,
    now: Optional[datetime] = None,
) -> date:
    """Return the most recently completed trading day.

    **Time-aware behaviour (production):** when neither *today* nor *now* is
    supplied the function reads the real US-Eastern clock.  If it is at or
    after 4:00 PM ET on a weekday the market has closed for that day, so
    today itself is the most recently completed trading day.  Before 4 PM the
    previous business day is returned as before.

    **Date-only / legacy path:** when *today* is supplied as a plain ``date``
    object the old behaviour is preserved (always return the previous business
    day) so that existing tests that pass a date continue to pass unchanged.

    **Datetime injection (tests for new behaviour):** pass *now* as a
    timezone-aware (or naive-ET-assumed) ``datetime`` to simulate a specific
    wall-clock time without touching *today*.

    Weekends are skipped; holidays are not accounted for.
    """
    if today is not None:
        # Legacy path — date-only, always return the previous business day.
        candidate = today - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate

    # Time-aware path.
    et = (now or _et_now())
    if et.tzinfo is None:
        et = et.replace(tzinfo=_ET)
    else:
        et = et.astimezone(_ET)

    today_date = et.date()
    if today_date.weekday() < 5 and et.hour >= _MARKET_CLOSE_HOUR:
        return today_date

    candidate = today_date - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def last_completed_trading_week_end(
    today: Optional[date] = None,
    now: Optional[datetime] = None,
) -> date:
    """Return the Friday that ended the most recently completed trading week.

    **Time-aware behaviour (production):** when neither *today* nor *now* is
    supplied the function reads the real US-Eastern clock.  If today is a
    Friday and it is at or after 4:00 PM ET this week's close is complete, so
    today's date is returned.  For any other weekday, or before 4 PM on a
    Friday, the previous Friday is returned.  Saturday and Sunday always
    return the Friday that just passed.

    **Date-only / legacy path:** when *today* is supplied as a plain ``date``
    object the old behaviour is preserved so existing tests continue to pass.

    **Datetime injection (tests for new behaviour):** pass *now* as a
    timezone-aware (or naive-ET-assumed) ``datetime``.

    Holidays are not accounted for.
    """
    if today is not None:
        # Legacy path.
        days_since_friday = (today.weekday() - 4) % 7
        if days_since_friday == 0 and today.weekday() == 4:
            days_since_friday = 7
        return today - timedelta(days=days_since_friday)

    # Time-aware path.
    et = (now or _et_now())
    if et.tzinfo is None:
        et = et.replace(tzinfo=_ET)
    else:
        et = et.astimezone(_ET)

    today_date = et.date()

    # Friday after close → this week is complete.
    if today_date.weekday() == 4 and et.hour >= _MARKET_CLOSE_HOUR:
        return today_date

    days_since_friday = (today_date.weekday() - 4) % 7
    if days_since_friday == 0 and today_date.weekday() == 4:
        # Before-close Friday handled above; this branch is unreachable but
        # kept for clarity.
        days_since_friday = 7
    return today_date - timedelta(days=days_since_friday)


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
