"""Shared provider-agnostic market data exceptions."""


class MarketDataError(Exception):
    """Base exception for market data API errors."""


class MarketDataThrottled(MarketDataError):
    """Raised when a market data API returns a rate-limit / throttle error."""


class MarketDataSymbolNotFound(MarketDataError):
    """Raised when a market data API has no data for the requested symbol."""
