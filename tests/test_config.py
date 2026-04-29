"""Tests for the config module."""

import os

import pytest

from app.config import (
    get_alpha_vantage_api_key,
    get_database_url,
    get_log_level,
    is_postgres,
    require_alpha_vantage_api_key,
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
