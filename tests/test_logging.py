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

        logging.getLogger().setLevel(logging.DEBUG)

    @pytest.mark.parametrize(
        "logger_name",
        ["urllib3", "requests", "httpcore", "httpx", "http.client"],
    )
    def test_http_logger_is_at_warning_or_above(self, logger_name):
        logger = logging.getLogger(logger_name)
        assert logger.level == logging.WARNING, (
            f"Logger '{logger_name}' is explicitly set to "
            f"{logging.getLevelName(logger.level)}, expected WARNING to prevent "
            f"API key leakage"
        )

    def test_app_logger_is_not_suppressed(self):
        """App loggers should remain at the configured level, not pinned to WARNING."""
        app_logger = logging.getLogger("app.alpha_vantage_client")
        assert app_logger.level == logging.NOTSET, (
            f"App logger is explicitly set to {logging.getLevelName(app_logger.level)}, "
            f"it should remain NOTSET so it is not suppressed like HTTP loggers"
        )
