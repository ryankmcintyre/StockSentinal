"""Low-level Alpha Vantage API client.

Centralizes HTTP requests, URL building, response parsing, and
Alpha Vantage-specific error handling (throttling, missing symbols).
"""

import logging
import time as _time
from dataclasses import dataclass
from datetime import date

import requests

BASE_URL = "https://www.alphavantage.co/query"
REQUEST_TIMEOUT = 30  # seconds

logger = logging.getLogger(__name__)


class AlphaVantageError(Exception):
    """Base exception for Alpha Vantage API errors."""


class AlphaVantageThrottled(AlphaVantageError):
    """Raised when the API returns a rate-limit / throttle note."""


class AlphaVantageSymbolNotFound(AlphaVantageError):
    """Raised when the API has no data for the requested symbol."""


@dataclass
class DailyBar:
    """A single daily OHLCV bar."""
    date: date
    close: float


@dataclass
class WeeklyBar:
    """A single weekly OHLCV bar."""
    date: date
    close: float


@dataclass
class SMAPoint:
    """A single SMA data point."""
    date: date
    sma: float


def _get(params: dict, api_key: str) -> dict:
    """Make a GET request to Alpha Vantage and return parsed JSON.

    Raises on HTTP errors, throttling notes, and error messages.
    """
    params["apikey"] = api_key
    # Log the request with the API key redacted
    safe_params = {k: v for k, v in params.items() if k != "apikey"}
    logger.debug("Alpha Vantage request: %s params=%s", BASE_URL, safe_params)

    start = _time.monotonic()
    resp = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    elapsed_ms = (_time.monotonic() - start) * 1000
    logger.debug("Alpha Vantage response: status=%d elapsed=%.0fms", resp.status_code, elapsed_ms)

    resp.raise_for_status()
    data = resp.json()

    # Alpha Vantage returns throttle messages as a "Note" key
    if "Note" in data:
        logger.warning("Alpha Vantage throttled: %s", data["Note"])
        raise AlphaVantageThrottled(data["Note"])

    # Error responses use "Error Message"
    if "Error Message" in data:
        logger.warning("Alpha Vantage error: %s", data["Error Message"])
        raise AlphaVantageSymbolNotFound(data["Error Message"])

    # Some error states use "Information" — treat any such response as
    # a throttle / error since valid responses never contain this key.
    if "Information" in data:
        logger.warning("Alpha Vantage throttled: %s", data["Information"])
        raise AlphaVantageThrottled(data["Information"])

    return data


def fetch_company_name(symbol: str, api_key: str) -> str:
    """Look up the company name for a ticker symbol via SYMBOL_SEARCH.

    Returns the best-match company name, or raises
    AlphaVantageSymbolNotFound if no matches are found.
    """
    params = {
        "function": "SYMBOL_SEARCH",
        "keywords": symbol,
    }
    data = _get(params, api_key)

    matches = data.get("bestMatches", [])
    if not matches:
        raise AlphaVantageSymbolNotFound(
            f"No matching company found for symbol '{symbol}'"
        )

    best = matches[0]
    if "2. name" not in best:
        raise AlphaVantageError(
            "Incomplete company information received from Alpha Vantage"
        )

    return best["2. name"]


def fetch_daily_series(symbol: str, api_key: str) -> list[DailyBar]:
    """Fetch compact daily time series (latest ~100 trading days).

    Returns bars sorted most-recent-first.
    """
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": symbol,
        "outputsize": "compact",
    }
    data = _get(params, api_key)

    ts_key = "Time Series (Daily)"
    if ts_key not in data:
        raise AlphaVantageError(
            f"Unexpected response structure: missing '{ts_key}' key"
        )

    bars: list[DailyBar] = []
    for date_str, values in data[ts_key].items():
        bars.append(DailyBar(
            date=date.fromisoformat(date_str),
            close=float(values["4. close"]),
        ))

    bars.sort(key=lambda b: b.date, reverse=True)
    logger.debug("Fetched %d daily bars for %s", len(bars), symbol)
    return bars


def fetch_weekly_series(symbol: str, api_key: str) -> list[WeeklyBar]:
    """Fetch weekly time series (full history).

    Returns bars sorted most-recent-first.
    """
    params = {
        "function": "TIME_SERIES_WEEKLY",
        "symbol": symbol,
    }
    data = _get(params, api_key)

    ts_key = "Weekly Time Series"
    if ts_key not in data:
        raise AlphaVantageError(
            f"Unexpected response structure: missing '{ts_key}' key"
        )

    bars: list[WeeklyBar] = []
    for date_str, values in data[ts_key].items():
        bars.append(WeeklyBar(
            date=date.fromisoformat(date_str),
            close=float(values["4. close"]),
        ))

    bars.sort(key=lambda b: b.date, reverse=True)
    logger.debug("Fetched %d weekly bars for %s", len(bars), symbol)
    return bars


def fetch_sma(
    symbol: str,
    interval: str,
    time_period: int,
    api_key: str,
) -> list[SMAPoint]:
    """Fetch SMA technical indicator values from Alpha Vantage.

    Args:
        symbol: Ticker symbol.
        interval: 'daily' or 'weekly'.
        time_period: Number of data points for the SMA (e.g. 21 or 20).
        api_key: Alpha Vantage API key.

    Returns SMA points sorted most-recent-first.
    """
    params = {
        "function": "SMA",
        "symbol": symbol,
        "interval": interval,
        "time_period": str(time_period),
        "series_type": "close",
    }
    data = _get(params, api_key)

    analysis_key = "Technical Analysis: SMA"
    if analysis_key not in data:
        raise AlphaVantageError(
            f"Unexpected response structure: missing '{analysis_key}' key"
        )

    points: list[SMAPoint] = []
    for date_str, values in data[analysis_key].items():
        points.append(SMAPoint(
            date=date.fromisoformat(date_str),
            sma=float(values["SMA"]),
        ))

    points.sort(key=lambda p: p.date, reverse=True)
    logger.debug("Fetched %d SMA-%d (%s) points for %s", len(points), time_period, interval, symbol)
    return points
