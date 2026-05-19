"""Tier configuration and limit enforcement helpers."""

from dataclasses import dataclass
from datetime import datetime, timezone

from app.models import User


@dataclass(frozen=True)
class TierLimits:
    max_tickers: int | None
    max_refreshes_per_day: int | None


DEFAULT_TIER = "free"
FULL_ACCESS_TIER = "full_access"

# Keep tier disclosure copy in app/templates/splash.html and app/templates/login.html
# in sync when these limits change.
TIER_LIMITS: dict[str, TierLimits] = {
    DEFAULT_TIER: TierLimits(max_tickers=5, max_refreshes_per_day=5),
    FULL_ACCESS_TIER: TierLimits(max_tickers=None, max_refreshes_per_day=None),
}


class TierLimitExceeded(Exception):
    """Raised when a user action would exceed the user's tier limits."""

    def __init__(self, message: str, *, remaining: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.remaining = remaining


def limits_for(user: User) -> TierLimits:
    """Return effective tier limits; admins are always uncapped."""
    if user.is_admin:
        return TierLimits(max_tickers=None, max_refreshes_per_day=None)
    return TIER_LIMITS.get(user.tier, TIER_LIMITS[DEFAULT_TIER])


def check_can_add_position(user: User, current_position_count: int) -> None:
    limits = limits_for(user)
    if limits.max_tickers is None:
        return
    if current_position_count >= limits.max_tickers:
        tier_label = user.tier.replace("_", " ")
        raise TierLimitExceeded(
            f"You've reached the {limits.max_tickers}-ticker limit on the {tier_label} tier.",
            remaining=0,
        )


def check_and_consume_refresh(user: User) -> None:
    """Check the daily refresh budget and increment it. Caller must commit."""
    limits = limits_for(user)
    if limits.max_refreshes_per_day is None:
        return

    today = datetime.now(timezone.utc).date()
    if user.refresh_count_date != today:
        user.refresh_count_date = today
        user.refresh_count_today = 0

    if user.refresh_count_today >= limits.max_refreshes_per_day:
        limit = limits.max_refreshes_per_day
        raise TierLimitExceeded(
            f"You've used {limit} of {limit} refreshes today. Your limit resets at midnight UTC.",
            remaining=0,
        )

    user.refresh_count_today += 1
