"""Application configuration loaded from environment variables."""

import os

from sqlalchemy.engine.url import make_url

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

_DEFAULT_SQLITE_URL = "sqlite:///./stocksentinal.db"


def get_database_url() -> str:
    """Return the database URL from DATABASE_URL env var.

    If not set, falls back to the default local SQLite file.
    """
    return os.environ.get("DATABASE_URL", _DEFAULT_SQLITE_URL)


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
