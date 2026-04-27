"""Market data service layer.

Orchestrates Alpha Vantage API calls, applies staleness checks so that
daily data is fetched at most once per completed trading day and weekly
data at most once per completed trading week, and persists the cached
snapshot back to SQLite.
"""

import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.alpha_vantage_client import (
    AlphaVantageError,
    fetch_company_name as _fetch_company_name,
    fetch_daily_series as _fetch_daily_series,
    fetch_sma as _fetch_sma,
    fetch_weekly_series as _fetch_weekly_series,
)
from app.config import require_alpha_vantage_api_key
from app.models import MarketIndicatorCache, Position
from app.schemas import InvestmentType

logger = logging.getLogger(__name__)


_ALPHA_VANTAGE_MIN_INTERVAL_SECONDS = 12.0
_last_alpha_vantage_call_at: Optional[float] = None
_rate_limit_lock = threading.Lock()


def _wait_for_alpha_vantage_slot() -> None:
    """Enforce the Alpha Vantage free-tier rate limit across all calls.

    Thread-safe: uses a lock so concurrent background tasks cannot
    bypass the rate limit.
    """
    global _last_alpha_vantage_call_at

    with _rate_limit_lock:
        now = time.monotonic()
        if _last_alpha_vantage_call_at is not None:
            elapsed = now - _last_alpha_vantage_call_at
            remaining = _ALPHA_VANTAGE_MIN_INTERVAL_SECONDS - elapsed
            if remaining > 0:
                logger.debug("Rate-limit: sleeping %.1fs before next API call", remaining)
                time.sleep(remaining)

        _last_alpha_vantage_call_at = time.monotonic()


def fetch_company_name(symbol: str, api_key: str) -> str:
    """Rate-limited wrapper for Alpha Vantage company name lookup."""
    _wait_for_alpha_vantage_slot()
    return _fetch_company_name(symbol, api_key)


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


def _indicator_cache_is_stale(
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
        target = _last_completed_trading_day(today)
        return cache_row.sma_date < target or cache_row.close_date != target
    elif interval == "weekly":
        target = _last_completed_trading_week_end(today)
        return cache_row.sma_date != target or cache_row.close_date != target
    return True


# ---------------------------------------------------------------------------
# Refresh logic
# ---------------------------------------------------------------------------


def _refresh_daily(position: Position, api_key: str) -> None:
    """Fetch and cache daily close + SMA-21 for a position."""
    symbol = position.ticker
    logger.info("Refreshing daily data for %s", symbol)

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
    logger.info("Refreshing weekly data for %s", symbol)

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

    # Fetch SMA-20 weekly (rate-limited by the fetch_sma wrapper)
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


def _needs_weekly(position: Position) -> bool:
    """Return True if this position uses weekly data (long-term only)."""
    return position.investment_type == InvestmentType.long_term


def _copy_daily_cache(source: Position, target: Position) -> None:
    """Copy cached daily market data fields from source to target."""
    target.daily_close = source.daily_close
    target.daily_sma_21 = source.daily_sma_21
    target.daily_market_date = source.daily_market_date
    target.daily_retrieved_at = source.daily_retrieved_at


def _copy_weekly_cache(source: Position, target: Position) -> None:
    """Copy cached weekly market data fields from source to target."""
    target.weekly_close = source.weekly_close
    target.weekly_sma_20 = source.weekly_sma_20
    target.weekly_market_date = source.weekly_market_date
    target.weekly_retrieved_at = source.weekly_retrieved_at


# ---------------------------------------------------------------------------
# Indicator cache refresh (for arbitrary MA periods)
# ---------------------------------------------------------------------------


def _refresh_indicator_cache_entry(
    db: Session,
    ticker: str,
    interval: str,
    time_period: int,
    api_key: str,
    close_cache: dict[tuple[str, str], tuple[float, date]],
) -> None:
    """Fetch and upsert one indicator cache entry.

    Uses close_cache to avoid re-fetching the close price for the same
    (ticker, interval) pair.
    """
    # Get or fetch close for this (ticker, interval)
    close_key = (ticker, interval)
    if close_key not in close_cache:
        if interval == "daily":
            bars = fetch_daily_series(ticker, api_key)
            if bars:
                close_cache[close_key] = (bars[0].close, bars[0].date)
        elif interval == "weekly":
            bars = fetch_weekly_series(ticker, api_key)
            if bars:
                target_friday = _last_completed_trading_week_end()
                bar = bars[0]
                if bar.date > target_friday and len(bars) > 1:
                    bar = bars[1]
                close_cache[close_key] = (bar.close, bar.date)

    close_val, close_date = close_cache.get(close_key, (None, None))

    # Fetch SMA
    sma_val = None
    sma_date = None
    sma_points = fetch_sma(ticker, interval=interval, time_period=time_period, api_key=api_key)
    if sma_points:
        if interval == "weekly":
            target_friday = _last_completed_trading_week_end()
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

    # Upsert cache row
    row = (
        db.query(MarketIndicatorCache)
        .filter(MarketIndicatorCache.ticker == ticker)
        .filter(MarketIndicatorCache.interval == interval)
        .filter(MarketIndicatorCache.time_period == time_period)
        .first()
    )
    if row is None:
        row = MarketIndicatorCache(
            ticker=ticker,
            interval=interval,
            time_period=time_period,
        )
        db.add(row)

    row.sma_value = sma_val
    row.sma_date = sma_date
    row.close_value = close_val
    row.close_date = close_date
    row.retrieved_at = datetime.now()


def refresh_indicator_cache(
    db: Session,
    tickers: set[str],
    required_indicators: set[tuple[str, int]],
    force: bool = False,
) -> list[str]:
    """Refresh the indicator cache for the given tickers and indicators.

    Args:
        tickers: Set of ticker symbols to refresh.
        required_indicators: Set of (interval, time_period) needed by rules.
        force: If True, refresh even if cache is fresh.

    Returns list of error messages (empty on full success).
    """
    if not tickers or not required_indicators:
        return []

    api_key = require_alpha_vantage_api_key()
    errors: list[str] = []
    # Deduplication: one close fetch per (ticker, interval)
    close_cache: dict[tuple[str, str], tuple[float, date]] = {}

    for ticker in sorted(tickers):
        for interval, time_period in sorted(required_indicators):
            # Check staleness
            if not force:
                existing = (
                    db.query(MarketIndicatorCache)
                    .filter(MarketIndicatorCache.ticker == ticker)
                    .filter(MarketIndicatorCache.interval == interval)
                    .filter(MarketIndicatorCache.time_period == time_period)
                    .first()
                )
                if not _indicator_cache_is_stale(existing, interval):
                    continue

            try:
                _refresh_indicator_cache_entry(
                    db, ticker, interval, time_period, api_key, close_cache,
                )
            except Exception as exc:
                msg = f"Indicator cache refresh failed for {ticker} {interval} SMA-{time_period}: {exc}"
                logger.exception(msg)
                errors.append(msg)

    db.commit()
    return errors


def load_indicator_cache_for_tickers(
    db: Session,
    tickers: set[str],
) -> dict[str, dict[tuple[str, int], tuple[Optional[float], Optional[float]]]]:
    """Load the indicator cache for a set of tickers.

    Returns: {ticker: {(interval, time_period): (close_value, sma_value)}}
    """
    if not tickers:
        return {}

    rows = (
        db.query(MarketIndicatorCache)
        .filter(MarketIndicatorCache.ticker.in_(tickers))
        .all()
    )

    result: dict[str, dict[tuple[str, int], tuple[Optional[float], Optional[float]]]] = {}
    for row in rows:
        ticker_signals = result.setdefault(row.ticker, {})
        ticker_signals[(row.interval, row.time_period)] = (row.close_value, row.sma_value)

    return result


# ---------------------------------------------------------------------------
# Position refresh
# ---------------------------------------------------------------------------


def refresh_position(position: Position, db: Session, force: bool = False) -> None:
    """Refresh cached market data for a single position.

    Respects staleness checks unless *force* is True. Weekly data is only
    fetched for long-term positions. Errors are stored in
    position.refresh_error so the UI can report them without crashing.

    Also refreshes the indicator cache for any configured MA conditions.
    """
    api_key = require_alpha_vantage_api_key()
    errors: list[str] = []

    # Daily refresh (needed by both short-term and long-term positions)
    if force or daily_data_is_stale(position):
        logger.debug("%s daily data is stale, refreshing", position.ticker)
        try:
            _refresh_daily(position, api_key)
        except Exception as exc:
            logger.exception("Daily refresh failed for %s", position.ticker)
            errors.append(f"Daily refresh failed: {exc}")
    else:
        logger.debug("%s daily data is fresh, skipping", position.ticker)

    # Weekly refresh (only for long-term positions)
    if _needs_weekly(position):
        if force or weekly_data_is_stale(position):
            logger.debug("%s weekly data is stale, refreshing", position.ticker)
            try:
                _refresh_weekly(position, api_key)
            except Exception as exc:
                logger.exception("Weekly refresh failed for %s: %s", position.ticker, exc)
                errors.append(f"Weekly refresh failed: {exc}")
        else:
            logger.debug("%s weekly data is fresh, skipping", position.ticker)
    else:
        logger.debug("%s is short-term, skipping weekly refresh", position.ticker)

    # Refresh indicator cache for configured rules
    from app.rule_config import get_required_indicators
    required = get_required_indicators(db)
    if required:
        cache_errors = refresh_indicator_cache(
            db, {position.ticker}, required, force=force,
        )
        errors.extend(cache_errors)

    position.refresh_error = "; ".join(errors) if errors else None
    db.commit()


def refresh_all_positions(db: Session, force: bool = False) -> int:
    """Refresh cached market data for all positions.

    Deduplicates API calls: positions sharing the same ticker are grouped
    so that each unique ticker is fetched at most once. The cached data is
    then copied to all positions in the group.

    Also refreshes the indicator cache for configured MA conditions.

    Returns the number of positions that were actually refreshed.
    """
    positions = db.query(Position).all()
    logger.info("Starting refresh for %d positions (force=%s)", len(positions), force)

    # Group positions by ticker
    ticker_groups: dict[str, list[Position]] = {}
    for pos in positions:
        ticker_groups.setdefault(pos.ticker, []).append(pos)

    refreshed = 0
    for ticker, group in ticker_groups.items():
        # Determine what data this ticker group needs
        daily_needs = {id(p): force or daily_data_is_stale(p) for p in group}
        weekly_needs = {
            id(p): (force or weekly_data_is_stale(p)) and _needs_weekly(p)
            for p in group
        }
        group_needs_daily = any(daily_needs.values())
        group_needs_weekly = any(weekly_needs.values())

        if not group_needs_daily and not group_needs_weekly:
            continue

        # Pick a representative position to refresh — prefer a long-term
        # position so weekly data is fetched when any group member needs it
        representative = group[0]
        if group_needs_weekly:
            for p in group:
                if _needs_weekly(p):
                    representative = p
                    break

        # Refresh the representative
        api_key = require_alpha_vantage_api_key()
        errors: list[str] = []

        daily_ok = False
        if group_needs_daily:
            try:
                _refresh_daily(representative, api_key)
                daily_ok = True
            except Exception as exc:
                logger.exception("Daily refresh failed for %s", ticker)
                errors.append(f"Daily refresh failed: {exc}")

        weekly_ok = False
        if group_needs_weekly:
            try:
                _refresh_weekly(representative, api_key)
                weekly_ok = True
            except Exception as exc:
                logger.exception("Weekly refresh failed for %s", ticker)
                errors.append(f"Weekly refresh failed: {exc}")

        representative.refresh_error = "; ".join(errors) if errors else None

        # Copy cached data from the representative to other positions
        for pos in group:
            if pos is representative:
                refreshed += 1
                continue
            copied_daily = group_needs_daily and daily_ok
            copied_weekly = group_needs_weekly and _needs_weekly(pos) and weekly_ok

            if copied_daily:
                _copy_daily_cache(representative, pos)
            if copied_weekly:
                _copy_weekly_cache(representative, pos)

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

    # Refresh indicator cache for all tickers and configured conditions
    from app.rule_config import get_required_indicators
    required = get_required_indicators(db)
    if required and ticker_groups:
        all_tickers = set(ticker_groups.keys())
        cache_errors = refresh_indicator_cache(db, all_tickers, required, force=force)
        if cache_errors:
            logger.warning("Indicator cache refresh errors: %s", cache_errors)

    logger.info("Refresh complete: %d/%d positions refreshed", refreshed, len(positions))
    return refreshed
