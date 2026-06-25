"""US equities trading-day calendar helpers.

Wraps pandas-market-calendars to keep its (somewhat heavy) imports off the
hot path. The nightly refresh job uses this to skip weekends and US market
holidays even if its external scheduler fires on those days.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache


@lru_cache(maxsize=1)
def _xnys_calendar():
    # Imported lazily so pandas only loads when the nightly job (or tests)
    # actually need calendar lookups.
    import pandas_market_calendars as mcal

    return mcal.get_calendar("XNYS")


def is_trading_day(d: date) -> bool:
    """Return True if *d* is a regular NYSE trading day.

    Returns False for weekends and US market holidays (e.g. Thanksgiving,
    Christmas, Memorial Day, etc.). Early-close days (e.g. day after
    Thanksgiving) are still considered trading days.
    """
    # Quick reject for weekends so we never even import pandas when the
    # scheduler fires on a Saturday/Sunday.
    if d.weekday() >= 5:
        return False
    calendar = _xnys_calendar()
    schedule = calendar.schedule(start_date=d, end_date=d)
    return not schedule.empty
