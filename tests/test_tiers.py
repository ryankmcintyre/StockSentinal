from datetime import date, datetime, timezone

import pytest

from app.models import User
from app.tiers import (
    TierLimitExceeded,
    check_and_consume_refresh,
    check_can_add_position,
    limits_for,
)


def _user(**overrides) -> User:
    values = {
        "id": "user-id",
        "email": "user@example.com",
        "tier": "free",
        "is_admin": False,
        "refresh_count_today": 0,
        "refresh_count_date": None,
    }
    values.update(overrides)
    return User(**values)


def test_free_user_can_add_under_limit():
    check_can_add_position(_user(), 4)


def test_free_user_cannot_add_at_limit():
    with pytest.raises(TierLimitExceeded) as excinfo:
        check_can_add_position(_user(), 5)

    assert "5-ticker limit" in excinfo.value.message
    assert excinfo.value.remaining == 0


def test_full_access_and_admin_bypass_ticker_limit():
    check_can_add_position(_user(tier="full_access"), 50)
    check_can_add_position(_user(is_admin=True), 50)


def test_free_refresh_increments_until_limit():
    today = datetime.now(timezone.utc).date()
    user = _user(refresh_count_today=4, refresh_count_date=today)

    check_and_consume_refresh(user)

    assert user.refresh_count_today == 5
    assert user.refresh_count_date == today


def test_free_refresh_blocks_over_limit():
    user = _user(refresh_count_today=5, refresh_count_date=datetime.now(timezone.utc).date())

    with pytest.raises(TierLimitExceeded) as excinfo:
        check_and_consume_refresh(user)

    assert "used 5 of 5 refreshes today" in excinfo.value.message
    assert user.refresh_count_today == 5


def test_refresh_counter_resets_on_new_utc_day(monkeypatch):
    class FakeDateTime:
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc)

    monkeypatch.setattr("app.tiers.datetime", FakeDateTime)
    user = _user(refresh_count_today=5, refresh_count_date=date(2026, 5, 19))

    check_and_consume_refresh(user)

    assert user.refresh_count_today == 1
    assert user.refresh_count_date == date(2026, 5, 20)


def test_full_access_and_admin_bypass_refresh_limit():
    today = datetime.now(timezone.utc).date()
    full = _user(tier="full_access", refresh_count_today=5, refresh_count_date=today)
    admin = _user(is_admin=True, refresh_count_today=5, refresh_count_date=today)

    check_and_consume_refresh(full)
    check_and_consume_refresh(admin)

    assert full.refresh_count_today == 5
    assert admin.refresh_count_today == 5
    assert limits_for(admin).max_tickers is None
