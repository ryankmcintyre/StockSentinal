"""Nightly market data refresh job for full-access users.

Run once per US-market trading day (after regular-hours close) via an external
scheduler — see ``render.yaml`` for the Render Cron Job definition.

Entry point::

    python -m app.jobs.nightly_refresh

Behaviour:

* Skips weekends and US market holidays via :mod:`app.market_calendar`.
* Refreshes positions only for users whose ``tier == 'full_access'``. Free
  tier users (including free-tier admins) are excluded.
* Batches all eligible users into a single
  :func:`MarketDataService.refresh_all_positions` call so duplicate tickers
  across users share fetches and minimize provider API calls.
* Does not send emails. Does not consume per-user refresh quota. Does not
  toggle the per-position ``refresh_in_progress`` UI flag — this is a
  background system job, not a user-initiated refresh.
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.config import get_log_level, get_market_data_api_key
from app.database import SessionLocal
from app.logging_utils import (
    configure_refresh_logging,
    new_refresh_id,
    refresh_logging_context,
)
from app.market_calendar import is_trading_day
from app.market_data.service import MarketDataService
from app.market_data.provider import AlphaVantageProvider, TwelveDataProvider
from app.config import get_market_data_provider
from app.tiers import FULL_ACCESS_TIER
from app.unit_of_work import SqlAlchemyUnitOfWork


logger = logging.getLogger(__name__)


def _build_market_service() -> MarketDataService:
    """Construct the production market data service (matches app.main)."""
    if get_market_data_provider() == "twelvedata":
        provider = TwelveDataProvider()
    else:
        provider = AlphaVantageProvider()
    return MarketDataService(provider)


def run_nightly_refresh(
    session_factory: Callable[[], Session],
    market_service: MarketDataService,
    *,
    today: Optional[date] = None,
) -> int:
    """Execute the nightly refresh.

    Returns the number of positions refreshed (0 on a no-op skip). Exposed
    separately from :func:`main` so tests can drive it with an in-memory
    SQLAlchemy session factory.
    """
    today = today or datetime.now(timezone.utc).date()

    if not is_trading_day(today):
        logger.info("Nightly refresh skipped: %s is not a trading day", today)
        return 0

    session = session_factory()
    try:
        # System-level UoW: no user_id, so per-user RLS GUC stays anonymous.
        # The refresh service applies its own user filter below.
        uow = SqlAlchemyUnitOfWork(session, user_id=None)
        eligible_user_ids = uow.users.list_ids_by_tier(FULL_ACCESS_TIER)
        if not eligible_user_ids:
            logger.info("Nightly refresh skipped: no full_access users")
            return 0

        logger.info(
            "Nightly refresh starting for %d full_access users",
            len(eligible_user_ids),
        )
        refreshed = market_service.refresh_all_positions(
            session,
            force=False,
            user_ids=set(eligible_user_ids),
        )
        logger.info(
            "Nightly refresh complete: %d positions refreshed across %d users",
            refreshed,
            len(eligible_user_ids),
        )
        return refreshed
    finally:
        session.close()


def main() -> int:
    """CLI entry point. Returns a process exit code."""
    configure_refresh_logging()
    logging.basicConfig(
        level=get_log_level(),
        format="%(asctime)s [%(levelname)s] %(refresh_prefix)s%(name)s: %(message)s",
    )

    if not get_market_data_api_key():
        logger.warning(
            "Nightly refresh skipped: no market data API key configured"
        )
        return 0

    refresh_id = new_refresh_id()
    with refresh_logging_context(refresh_id):
        try:
            run_nightly_refresh(SessionLocal, _build_market_service())
        except Exception:
            logger.exception("Nightly refresh failed")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
