"""Application configuration loaded from environment variables."""

import os

from sqlalchemy.engine.url import make_url

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_MARKET_DATA_PROVIDERS = {"alphavantage", "twelvedata"}

_DEFAULT_SQLITE_URL = "sqlite:///./stocksentinal.db"


def get_database_url() -> str:
    """Return the database URL from DATABASE_URL env var.

    If not set or empty, falls back to the default local SQLite file.
    """
    url = os.environ.get("DATABASE_URL", "").strip()
    return url if url else _DEFAULT_SQLITE_URL


def is_postgres(url: str | None = None) -> bool:
    """Return True if the given (or configured) database URL targets PostgreSQL.

    Uses SQLAlchemy URL parsing to handle all driver variants
    (e.g. postgresql+psycopg2, postgresql+asyncpg).
    """
    if url is None:
        url = get_database_url()
    return make_url(url).get_backend_name() == "postgresql"


def get_log_level() -> str:
    """Return the configured log level from the LOG_LEVEL environment variable.

    Defaults to INFO. Invalid values are silently replaced with INFO.
    """
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if level not in _VALID_LOG_LEVELS:
        return "INFO"
    return level


def get_alpha_vantage_api_key() -> str | None:
    """Return the Alpha Vantage API key from the environment, or None if not set."""
    return os.environ.get("ALPHA_VANTAGE_API_KEY")


def require_alpha_vantage_api_key() -> str:
    """Return the Alpha Vantage API key, raising if it is not configured."""
    key = get_alpha_vantage_api_key()
    if not key:
        raise RuntimeError(
            "ALPHA_VANTAGE_API_KEY environment variable is not set. "
            "Set it in your environment or in a local .env file."
        )
    return key


def get_twelve_data_api_key() -> str | None:
    """Return the Twelve Data API key from the environment, or None if not set."""
    return os.environ.get("TWELVE_DATA_API_KEY")


def require_twelve_data_api_key() -> str:
    """Return the Twelve Data API key, raising if it is not configured."""
    key = get_twelve_data_api_key()
    if not key:
        raise RuntimeError(
            "TWELVE_DATA_API_KEY environment variable is not set. "
            "Set it in your environment or in a local .env file."
        )
    return key


def get_market_data_provider() -> str:
    """Return the configured market data provider name.

    Defaults to Alpha Vantage. Unknown values fall back to Alpha Vantage
    so the application remains backward-compatible with older deployments.
    """
    provider = os.environ.get("MARKET_DATA_PROVIDER", "alphavantage").strip().lower()
    if provider not in _VALID_MARKET_DATA_PROVIDERS:
        return "alphavantage"
    return provider


def get_market_data_api_key() -> str | None:
    """Return the API key for the configured market data provider."""
    if get_market_data_provider() == "twelvedata":
        return get_twelve_data_api_key()
    return get_alpha_vantage_api_key()


def get_market_data_api_key_env_var() -> str:
    """Return the API-key environment variable name for the active provider."""
    if get_market_data_provider() == "twelvedata":
        return "TWELVE_DATA_API_KEY"
    return "ALPHA_VANTAGE_API_KEY"


def get_market_data_provider_display_name() -> str:
    """Return a human-readable label for the configured provider."""
    if get_market_data_provider() == "twelvedata":
        return "Twelve Data"
    return "Alpha Vantage"
