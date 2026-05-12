"""Low-level Twelve Data API client.

Centralizes HTTP requests, URL building, response parsing, and
Twelve Data-specific response handling.
"""

import logging
import time as _time
from datetime import date
from typing import Any

import requests

from app.alpha_vantage_client import (
    ATRPoint,
    AlphaVantageError,
    AlphaVantageSymbolNotFound,
    AlphaVantageThrottled,
    DailyBar,
    SMAPoint,
    SymbolSearchMatch,
    WeeklyBar,
)

BASE_URL = "https://api.twelvedata.com"
REQUEST_TIMEOUT = 30  # seconds

logger = logging.getLogger(__name__)

_INTERVAL_MAP = {
    "daily": "1day",
    "weekly": "1week",
    "1day": "1day",
    "1week": "1week",
}


def _parse_date(value: str) -> date:
    """Parse a Twelve Data date/datetime string into a date."""
    return date.fromisoformat(value.split("T", 1)[0].split(" ", 1)[0])


def _normalize_interval(interval: str) -> str:
    """Convert app interval names to Twelve Data interval values."""
    return _INTERVAL_MAP.get(interval, interval)


def _raise_api_error(data: dict[str, Any]) -> None:
    """Raise a provider-specific exception from a Twelve Data error payload."""
    message = str(data.get("message") or "Unexpected Twelve Data API error")
    code = str(data.get("code") or "")
    lowered = message.lower()

    if code == "429" or "rate limit" in lowered or "api credits" in lowered:
        logger.warning("Twelve Data throttled: %s", message)
        raise AlphaVantageThrottled(message)

    if "symbol" in lowered and any(term in lowered for term in ("not found", "invalid", "missing")):
        logger.warning("Twelve Data symbol error: %s", message)
        raise AlphaVantageSymbolNotFound(message)

    logger.warning("Twelve Data error: %s", message)
    raise AlphaVantageError(message)


def _get(path: str, params: dict[str, Any], api_key: str) -> Any:
    """Make a GET request to Twelve Data and return parsed JSON."""
    safe_params = dict(params)
    request_params = {**safe_params, "apikey": api_key}
    url = f"{BASE_URL}{path}"
    logger.debug("Twelve Data request: %s params=%s", url, safe_params)

    start = _time.monotonic()
    resp = requests.get(url, params=request_params, timeout=REQUEST_TIMEOUT)
    elapsed_ms = (_time.monotonic() - start) * 1000
    logger.debug("Twelve Data response: status=%d elapsed=%.0fms", resp.status_code, elapsed_ms)

    resp.raise_for_status()
    data = resp.json()

    if isinstance(data, dict) and data.get("status") == "error":
        _raise_api_error(data)

    return data


def fetch_company_name(symbol: str, api_key: str) -> str:
    """Look up the company name for a ticker symbol via Twelve Data."""
    matches = fetch_ticker_matches(symbol, api_key)
    symbol_upper = symbol.upper()
    best = next(
        (item for item in matches if item.symbol.upper() == symbol_upper),
        matches[0],
    )
    return best.name


def fetch_ticker_matches(symbol: str, api_key: str) -> list[SymbolSearchMatch]:
    """Look up available ticker matches via Twelve Data."""
    data = _get("/stocks", {"symbol": symbol}, api_key)
    if isinstance(data, dict):
        matches = data.get("data", [])
    elif isinstance(data, list):
        matches = data
    else:
        matches = []

    if not matches:
        raise AlphaVantageSymbolNotFound(
            f"No matching company found for symbol '{symbol}'"
        )

    parsed_matches: list[SymbolSearchMatch] = []
    for match in matches:
        match_symbol = match.get("symbol")
        match_name = match.get("name") or match.get("instrument_name")
        if not match_symbol or not match_name:
            continue
        parsed_matches.append(
            SymbolSearchMatch(
                symbol=match_symbol,
                name=match_name,
                region=match.get("country"),
                type=match.get("type"),
            )
        )

    if not parsed_matches:
        raise AlphaVantageError(
            "Incomplete company information received from Twelve Data"
        )

    return parsed_matches


def _parse_bars(data: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    """Return sorted raw time-series bars from a Twelve Data response."""
    values = data.get("values", [])
    if not values:
        raise AlphaVantageSymbolNotFound(
            f"No time series data found for symbol '{symbol}'"
        )

    parsed = list(values)
    parsed.sort(key=lambda value: _parse_date(value["datetime"]), reverse=True)
    return parsed


def fetch_daily_series(symbol: str, api_key: str) -> list[DailyBar]:
    """Fetch daily time series bars from Twelve Data."""
    data = _get("/time_series", {"symbol": symbol, "interval": "1day"}, api_key)
    values = _parse_bars(data, symbol)
    return [
        DailyBar(
            date=_parse_date(value["datetime"]),
            close=float(value["close"]),
        )
        for value in values
    ]


def fetch_weekly_series(symbol: str, api_key: str) -> list[WeeklyBar]:
    """Fetch weekly time series bars from Twelve Data."""
    data = _get("/time_series", {"symbol": symbol, "interval": "1week"}, api_key)
    values = _parse_bars(data, symbol)
    return [
        WeeklyBar(
            date=_parse_date(value["datetime"]),
            close=float(value["close"]),
            open=float(value["open"]) if "open" in value else None,
            high=float(value["high"]) if "high" in value else None,
            low=float(value["low"]) if "low" in value else None,
            volume=float(value["volume"]) if "volume" in value else None,
        )
        for value in values
    ]


def fetch_sma(
    symbol: str,
    interval: str,
    time_period: int,
    api_key: str,
) -> list[SMAPoint]:
    """Fetch SMA technical indicator values from Twelve Data."""
    data = _get(
        "/sma",
        {
            "symbol": symbol,
            "interval": _normalize_interval(interval),
            "time_period": str(time_period),
        },
        api_key,
    )

    values = data.get("values", [])
    if not values:
        raise AlphaVantageError("Unexpected response structure: missing 'values' key")

    points = [
        SMAPoint(
            date=_parse_date(value["datetime"]),
            sma=float(value["sma"]),
        )
        for value in values
    ]
    points.sort(key=lambda point: point.date, reverse=True)
    logger.debug(
        "Fetched %d SMA-%d (%s) points for %s from Twelve Data",
        len(points), time_period, interval, symbol,
    )
    return points


def fetch_atr(
    symbol: str,
    interval: str,
    time_period: int,
    api_key: str,
) -> list[ATRPoint]:
    """Fetch ATR technical indicator values from Twelve Data."""
    data = _get(
        "/atr",
        {
            "symbol": symbol,
            "interval": _normalize_interval(interval),
            "time_period": str(time_period),
        },
        api_key,
    )

    values = data.get("values", [])
    if not values:
        raise AlphaVantageError("Unexpected response structure: missing 'values' key")

    points = [
        ATRPoint(
            date=_parse_date(value["datetime"]),
            atr=float(value["atr"]),
        )
        for value in values
    ]
    points.sort(key=lambda point: point.date, reverse=True)
    logger.debug(
        "Fetched %d ATR-%d (%s) points for %s from Twelve Data",
        len(points), time_period, interval, symbol,
    )
    return points
