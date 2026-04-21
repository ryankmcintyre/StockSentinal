"""Tests for the config module."""

import os

import pytest

from app.config import get_alpha_vantage_api_key, require_alpha_vantage_api_key


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
