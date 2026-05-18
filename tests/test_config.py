"""Tests for the config module."""

import pytest

from app.config import (
    get_alpha_vantage_api_key,
    get_alpha_vantage_min_interval_seconds,
    get_database_url,
    get_log_level,
    get_market_data_api_key,
    get_market_data_api_key_env_var,
    get_market_data_provider,
    get_market_data_provider_display_name,
    get_supabase_publishable_key,
    get_twelve_data_api_key,
    get_twelve_data_min_interval_seconds,
    has_supabase_publishable_key,
    is_postgres,
    require_alpha_vantage_api_key,
    require_twelve_data_api_key,
)


class TestGetLogLevel:
    def test_defaults_to_info(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        assert get_log_level() == "INFO"

    def test_returns_configured_level(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert get_log_level() == "DEBUG"

    def test_normalizes_to_uppercase(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "warning")
        assert get_log_level() == "WARNING"

    def test_invalid_level_falls_back_to_info(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
        assert get_log_level() == "INFO"


class TestGetAlphaVantageApiKey:
    def test_returns_key_when_set(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test_key_123")
        assert get_alpha_vantage_api_key() == "test_key_123"

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
        assert get_alpha_vantage_api_key() is None


class TestRequireAlphaVantageApiKey:
    def test_returns_key_when_set(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "test_key_456")
        assert require_alpha_vantage_api_key() == "test_key_456"

    def test_raises_when_not_set(self, monkeypatch):
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="ALPHA_VANTAGE_API_KEY"):
            require_alpha_vantage_api_key()


class TestGetDatabaseUrl:
    def test_defaults_to_sqlite(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert get_database_url() == "sqlite:///./stocksentinal.db"

    def test_returns_env_value(self, monkeypatch):
        url = "postgresql+psycopg2://user:pass@host:6543/db"
        monkeypatch.setenv("DATABASE_URL", url)
        assert get_database_url() == url

    def test_empty_string_falls_back_to_sqlite(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "")
        assert get_database_url() == "sqlite:///./stocksentinal.db"

    def test_whitespace_only_falls_back_to_sqlite(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "   ")
        assert get_database_url() == "sqlite:///./stocksentinal.db"


class TestSupabasePublishableKey:
    def test_returns_key_when_set(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
        assert get_supabase_publishable_key() == "sb_publishable_test"

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
        assert get_supabase_publishable_key() is None

    def test_returns_none_when_whitespace_only(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "   ")
        assert get_supabase_publishable_key() is None

    def test_has_publishable_key_true_when_set(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test")
        assert has_supabase_publishable_key() is True

    def test_has_publishable_key_false_when_unset(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_PUBLISHABLE_KEY", raising=False)
        assert has_supabase_publishable_key() is False


class TestIsPostgres:
    def test_sqlite_url(self):
        assert is_postgres("sqlite:///./test.db") is False

    def test_postgres_psycopg2_url(self):
        assert is_postgres("postgresql+psycopg2://user:pass@host:5432/db") is True

    def test_postgres_plain_url(self):
        assert is_postgres("postgresql://user:pass@host:5432/db") is True

    def test_uses_env_when_no_arg(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert is_postgres() is False

    def test_uses_env_postgres(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/d")
        assert is_postgres() is True


class TestGetTwelveDataApiKey:
    def test_returns_key_when_set(self, monkeypatch):
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "test_key_123")
        assert get_twelve_data_api_key() == "test_key_123"

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
        assert get_twelve_data_api_key() is None

    def test_returns_none_when_whitespace_only(self, monkeypatch):
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "  \n\t ")
        assert get_twelve_data_api_key() is None


class TestRequireTwelveDataApiKey:
    def test_returns_key_when_set(self, monkeypatch):
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "test_key_456")
        assert require_twelve_data_api_key() == "test_key_456"

    def test_raises_when_not_set(self, monkeypatch):
        monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="TWELVE_DATA_API_KEY"):
            require_twelve_data_api_key()


class TestMarketDataRateLimitIntervals:
    def test_alpha_vantage_interval_defaults_to_free_tier_safe_value(self, monkeypatch):
        monkeypatch.delenv("ALPHA_VANTAGE_MIN_INTERVAL_SECONDS", raising=False)
        assert get_alpha_vantage_min_interval_seconds() == 12.0

    def test_alpha_vantage_interval_can_be_configured(self, monkeypatch):
        monkeypatch.setenv("ALPHA_VANTAGE_MIN_INTERVAL_SECONDS", "1.5")
        assert get_alpha_vantage_min_interval_seconds() == 1.5

    def test_twelve_data_interval_defaults_to_free_tier_safe_value(self, monkeypatch):
        monkeypatch.delenv("TWELVE_DATA_MIN_INTERVAL_SECONDS", raising=False)
        assert get_twelve_data_min_interval_seconds() == 8.0

    def test_twelve_data_interval_can_be_configured(self, monkeypatch):
        monkeypatch.setenv("TWELVE_DATA_MIN_INTERVAL_SECONDS", "0.25")
        assert get_twelve_data_min_interval_seconds() == 0.25

    def test_invalid_interval_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("TWELVE_DATA_MIN_INTERVAL_SECONDS", "-1")
        assert get_twelve_data_min_interval_seconds() == 8.0
        monkeypatch.setenv("ALPHA_VANTAGE_MIN_INTERVAL_SECONDS", "fast")
        assert get_alpha_vantage_min_interval_seconds() == 12.0


class TestGetMarketDataProvider:
    def test_defaults_to_alphavantage_when_no_keys_set(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
        assert get_market_data_provider() == "alphavantage"

    def test_auto_detects_twelvedata_from_key(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "td-key")
        assert get_market_data_provider() == "twelvedata"

    def test_auto_detects_twelvedata_when_both_keys_set(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "av-key")
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "td-key")
        assert get_market_data_provider() == "twelvedata"

    def test_does_not_auto_detect_twelvedata_from_whitespace_key(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "   ")
        assert get_market_data_provider() == "alphavantage"

    def test_explicit_provider_overrides_auto_detection(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "alphavantage")
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "td-key")
        assert get_market_data_provider() == "alphavantage"

    def test_normalizes_configured_provider(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "TwelveData")
        assert get_market_data_provider() == "twelvedata"

    def test_invalid_provider_falls_back_to_auto_detection(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "bloomberg")
        monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
        assert get_market_data_provider() == "alphavantage"

    def test_invalid_provider_auto_detects_twelvedata_from_key(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "bloomberg")
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "td-key")
        assert get_market_data_provider() == "twelvedata"


class TestMarketDataProviderHelpers:
    def test_returns_alpha_vantage_api_key_for_default_provider(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
        monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "av-key")
        assert get_market_data_api_key() == "av-key"
        assert get_market_data_api_key_env_var() == "ALPHA_VANTAGE_API_KEY"
        assert get_market_data_provider_display_name() == "Alpha Vantage"

    def test_auto_detects_twelvedata_when_only_td_key_set(self, monkeypatch):
        monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
        monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "td-key")
        assert get_market_data_api_key() == "td-key"
        assert get_market_data_provider_display_name() == "Twelve Data"

    def test_returns_twelve_data_api_key_for_twelvedata_provider(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_PROVIDER", "twelvedata")
        monkeypatch.setenv("TWELVE_DATA_API_KEY", "td-key")
        assert get_market_data_api_key() == "td-key"
        assert get_market_data_api_key_env_var() == "TWELVE_DATA_API_KEY"
        assert get_market_data_provider_display_name() == "Twelve Data"
