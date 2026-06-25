"""Tests for the nightly market data refresh job."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.jobs import nightly_refresh
from app.models import Base, Position, User


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    @event.listens_for(TestingSession.class_, "before_flush")
    def _assign_default_user(session, _flush_context, _instances):
        for obj in session.new:
            if isinstance(obj, Position) and obj.user_id is None:
                obj.user_id = "default-user"

    try:
        yield TestingSession
    finally:
        engine.dispose()


def _make_user(session_factory, *, user_id: str, tier: str) -> None:
    db = session_factory()
    try:
        db.add(
            User(
                id=user_id,
                email=f"{user_id}@example.com",
                display_name=user_id,
                created_at=datetime.now(timezone.utc),
                tier=tier,
            )
        )
        db.commit()
    finally:
        db.close()


def _add_position(session_factory, *, user_id: str, ticker: str) -> None:
    db = session_factory()
    try:
        db.add(
            Position(
                ticker=ticker,
                company_name=ticker,
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=110.0,
                notes=None,
                user_id=user_id,
            )
        )
        db.commit()
    finally:
        db.close()


def test_skips_non_trading_day(session_factory, mocker):
    """Weekends and holidays must be a no-op even if users exist."""
    mocker.patch("app.jobs.nightly_refresh.is_trading_day", return_value=False)
    _make_user(session_factory, user_id="u1", tier="full_access")
    _add_position(session_factory, user_id="u1", ticker="AAPL")
    service = MagicMock()

    result = nightly_refresh.run_nightly_refresh(
        session_factory, service, today=date(2025, 12, 25)
    )

    assert result == 0
    service.refresh_all_positions.assert_not_called()


def test_skips_when_no_full_access_users(session_factory, mocker):
    """Free-tier users (including admins) must be ignored."""
    mocker.patch("app.jobs.nightly_refresh.is_trading_day", return_value=True)
    _make_user(session_factory, user_id="free1", tier="free")
    _add_position(session_factory, user_id="free1", ticker="AAPL")
    service = MagicMock()

    result = nightly_refresh.run_nightly_refresh(
        session_factory, service, today=date(2025, 6, 16)
    )

    assert result == 0
    service.refresh_all_positions.assert_not_called()


def test_refreshes_only_full_access_users(session_factory, mocker):
    """Only positions belonging to full_access users are passed to the service."""
    mocker.patch("app.jobs.nightly_refresh.is_trading_day", return_value=True)
    _make_user(session_factory, user_id="paid1", tier="full_access")
    _make_user(session_factory, user_id="paid2", tier="full_access")
    _make_user(session_factory, user_id="free1", tier="free")
    _add_position(session_factory, user_id="paid1", ticker="AAPL")
    _add_position(session_factory, user_id="paid2", ticker="MSFT")
    _add_position(session_factory, user_id="free1", ticker="GOOG")

    service = MagicMock()
    service.refresh_all_positions.return_value = 2

    result = nightly_refresh.run_nightly_refresh(
        session_factory, service, today=date(2025, 6, 16)
    )

    assert result == 2
    assert service.refresh_all_positions.call_count == 1
    _args, kwargs = service.refresh_all_positions.call_args
    assert kwargs["user_ids"] == {"paid1", "paid2"}
    assert kwargs["force"] is False


def test_shared_ticker_across_users_batched_in_single_call(session_factory, mocker):
    """A ticker held by multiple full_access users still results in one batch."""
    mocker.patch("app.jobs.nightly_refresh.is_trading_day", return_value=True)
    _make_user(session_factory, user_id="paid1", tier="full_access")
    _make_user(session_factory, user_id="paid2", tier="full_access")
    _add_position(session_factory, user_id="paid1", ticker="AAPL")
    _add_position(session_factory, user_id="paid2", ticker="AAPL")

    service = MagicMock()
    service.refresh_all_positions.return_value = 2

    nightly_refresh.run_nightly_refresh(
        session_factory, service, today=date(2025, 6, 16)
    )

    # Service is invoked once with both user ids; the dedup-by-ticker logic
    # inside refresh_all_positions then ensures only one provider fetch per
    # unique ticker. We assert the single-call contract here and trust the
    # existing service-level tests to cover the dedup math.
    service.refresh_all_positions.assert_called_once()
    _args, kwargs = service.refresh_all_positions.call_args
    assert kwargs["user_ids"] == {"paid1", "paid2"}


def test_main_returns_zero_when_api_key_missing(mocker):
    mocker.patch(
        "app.jobs.nightly_refresh.get_market_data_api_key", return_value=None
    )
    run_mock = mocker.patch("app.jobs.nightly_refresh.run_nightly_refresh")

    assert nightly_refresh.main() == 0
    run_mock.assert_not_called()


def test_main_returns_one_on_unhandled_exception(mocker):
    mocker.patch(
        "app.jobs.nightly_refresh.get_market_data_api_key", return_value="key"
    )
    mocker.patch(
        "app.jobs.nightly_refresh._build_market_service", return_value=MagicMock()
    )
    mocker.patch(
        "app.jobs.nightly_refresh.run_nightly_refresh",
        side_effect=RuntimeError("boom"),
    )

    assert nightly_refresh.main() == 1
