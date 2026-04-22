"""Tests for logging configuration — ensures secrets are not leaked via third-party loggers."""

import logging

import pytest


class TestHttpLoggersSuppressed:
    """Third-party HTTP loggers must stay at WARNING or above to prevent
    leaking API keys in request URLs."""

    # These loggers are suppressed in app/main.py at import time.
    # Importing main triggers the logging configuration.
    @pytest.fixture(autouse=True)
    def _ensure_main_imported(self):
        import app.main  # noqa: F401

    @pytest.mark.parametrize(
        "logger_name",
        ["urllib3", "requests", "httpcore", "httpx", "http.client"],
    )
    def test_http_logger_is_at_warning_or_above(self, logger_name):
        level = logging.getLogger(logger_name).getEffectiveLevel()
        assert level >= logging.WARNING, (
            f"Logger '{logger_name}' is at {logging.getLevelName(level)}, "
            f"expected WARNING or above to prevent API key leakage"
        )

    def test_app_logger_is_not_suppressed(self):
        """App loggers should remain at the configured level, not pinned to WARNING."""
        app_logger = logging.getLogger("app.alpha_vantage_client")
        # App loggers should be below WARNING (i.e. they can emit INFO/DEBUG)
        assert app_logger.getEffectiveLevel() < logging.WARNING, (
            f"App logger is at {logging.getLevelName(app_logger.getEffectiveLevel())}, "
            f"it should not be suppressed like HTTP loggers"
        )
