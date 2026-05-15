"""Application configuration loaded from environment variables."""

import os

from sqlalchemy.engine.url import make_url

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_MARKET_DATA_PROVIDERS = {"alphavantage", "twelvedata"}

_DEFAULT_SQLITE_URL = "sqlite:///./stocksentinal.db"


def _get_env_var(name: str) -> str | None:
    """Return a trimmed env var value, or None if unset/empty."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


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
    return _get_env_var("ALPHA_VANTAGE_API_KEY")


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
    return _get_env_var("TWELVE_DATA_API_KEY")


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

    If MARKET_DATA_PROVIDER is explicitly set to a valid value, that provider
    is used. Otherwise the provider is auto-detected from whichever API key is
    present: Twelve Data takes precedence if TWELVE_DATA_API_KEY is set,
    otherwise Alpha Vantage is used as the default.
    """
    provider = os.environ.get("MARKET_DATA_PROVIDER", "").strip().lower()
    if provider in _VALID_MARKET_DATA_PROVIDERS:
        return provider
    # Auto-detect from available keys.
    if get_twelve_data_api_key():
        return "twelvedata"
    return "alphavantage"


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



def get_session_secret_key() -> str:
    """Return the secret key used to sign session cookies.

    When Supabase Auth is configured (SUPABASE_URL is set), SESSION_SECRET_KEY
    is required and the app raises at startup if it is missing — a misconfigured
    production environment must not silently use a known insecure default.

    When Supabase Auth is not configured (local dev without auth), an insecure
    fallback is used with a warning so the app still starts without extra setup.
    """
    import logging

    key = os.environ.get("SESSION_SECRET_KEY", "").strip()
    if key:
        return key
    # Auth is enabled in production — refuse to start with an insecure default.
    if os.environ.get("SUPABASE_URL", "").strip():
        raise RuntimeError(
            "SESSION_SECRET_KEY must be set when SUPABASE_URL is configured. "
            "Set it to a long random string in your environment or .env file."
        )
    logging.getLogger(__name__).warning(
        "SESSION_SECRET_KEY is not set — using insecure default. "
        "Set SESSION_SECRET_KEY in your .env for production."
    )
    return "dev-insecure-secret-change-me"


def get_supabase_url() -> str | None:
    """Return the Supabase project URL from environment, or None if not set."""
    url = os.environ.get("SUPABASE_URL", "").strip()
    return url if url else None


def get_supabase_jwks_url() -> str | None:
    """Return the Supabase JWKS discovery URL, or None if Supabase is unset."""
    url = get_supabase_url()
    if not url:
        return None
    return f"{url.rstrip('/')}/auth/v1/.well-known/jwks.json"


def has_session_secret_key() -> bool:
    """Return True when SESSION_SECRET_KEY is explicitly configured."""
    return bool(os.environ.get("SESSION_SECRET_KEY", "").strip())


def get_supabase_auth_providers() -> list[str]:
    """Return the list of enabled Supabase Auth social providers.

    Read from SUPABASE_AUTH_PROVIDERS env var as a comma-separated list.
    Defaults to ['google'] if not set.
    """
    raw = os.environ.get("SUPABASE_AUTH_PROVIDERS", "google").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]
