"""Tests for logging configuration — ensures secrets are not leaked via third-party loggers."""

import logging
import asyncio

import pytest

from app.logging_utils import configure_refresh_logging, refresh_logging_context


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

    @pytest.mark.parametrize(
        ("provider_name", "interval_seconds"),
        [("Twelve Data", 1.1), ("Alpha Vantage", 12.0)],
    )
    def test_startup_logs_active_market_data_rate_limit_interval(
        self, mocker, caplog, provider_name, interval_seconds
    ):
        import app.main

        mocker.patch("app.main.init_db")
        mocker.patch("app.main._clear_all_stale_refresh_flags", return_value=0)
        mocker.patch("app.main.get_market_data_api_key", return_value="configured-key")
        mocker.patch(
            "app.main.get_market_data_provider_display_name",
            return_value=provider_name,
        )
        mocker.patch(
            "app.main.get_market_data_min_interval_seconds",
            return_value=interval_seconds,
        )
        caplog.set_level(logging.INFO, logger="app.main")

        async def _run_lifespan():
            async with app.main.lifespan(app.main.app):
                pass

        asyncio.run(_run_lifespan())

        assert (
            f"Market data provider: {provider_name} "
            f"(rate-limit interval {interval_seconds:g}s between API calls)"
        ) in caplog.text

    def test_startup_logs_unconfigured_market_data_provider_without_api_key(
        self, mocker, caplog
    ):
        import app.main

        mocker.patch("app.main.init_db")
        mocker.patch("app.main._clear_all_stale_refresh_flags", return_value=0)
        mocker.patch("app.main.get_market_data_api_key", return_value=None)
        caplog.set_level(logging.INFO, logger="app.main")

        async def _run_lifespan():
            async with app.main.lifespan(app.main.app):
                pass

        asyncio.run(_run_lifespan())

        assert "Market data provider: unconfigured" in caplog.text
        assert "rate-limit interval" not in caplog.text

    def test_refresh_logging_context_populates_log_record_fields(self, caplog):
        logger = logging.getLogger("app.main")
        caplog.set_level(logging.INFO, logger="app.main")

        with refresh_logging_context("refresh-abcd"):
            logger.info("queued refresh")

        matching_records = [
            record for record in caplog.records
            if record.name == "app.main" and record.getMessage() == "queued refresh"
        ]
        assert matching_records
        assert matching_records[-1].refresh_id == "refresh-abcd"
        assert matching_records[-1].refresh_prefix == "[refresh-abcd] "

    def test_refresh_logging_without_context_uses_default_record_fields(self, caplog):
        logger = logging.getLogger("app.main")
        caplog.set_level(logging.INFO, logger="app.main")

        logger.info("outside refresh")

        matching_records = [
            record for record in caplog.records
            if record.name == "app.main" and record.getMessage() == "outside refresh"
        ]
        assert matching_records
        assert matching_records[-1].refresh_id == "-"
        assert matching_records[-1].refresh_prefix == ""

    def test_configure_refresh_logging_is_idempotent(self):
        initial_factory = logging.getLogRecordFactory()

        configure_refresh_logging()
        configure_refresh_logging()

        assert logging.getLogRecordFactory() is initial_factory
