"""Repository interfaces (Protocols) and SQLAlchemy implementations.

This module isolates all data-access queries behind repository abstractions
so that the application layer (routes, services) never imports or calls
SQLAlchemy directly.  The Protocol classes define the contract; the
``SqlAlchemy*`` classes are the default implementations backed by a
SQLAlchemy ``Session``.

Market-data cache access lives in ``app.market_data`` since it is tightly
coupled to the market data service.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Protocol, Sequence

from sqlalchemy.orm import Session

from app.models import (
    Position,
    PositionKeyLevel,
    StrategyRuleConfig,
    User,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position repository
# ---------------------------------------------------------------------------


class PositionRepository(Protocol):
    """Data-access contract for Position entities."""

    def list_all(self) -> Sequence[Position]: ...

    def list_all_ids(self) -> list[int]: ...

    def get_by_id(self, position_id: int) -> Optional[Position]: ...

    def get_by_ids(self, position_ids: list[int]) -> Sequence[Position]: ...

    def add(self, position: Position) -> None: ...

    def delete(self, position: Position) -> None: ...

    def refresh_instance(self, position: Position) -> None:
        """Re-read the instance from the database (e.g. to get auto-generated id)."""
        ...

    def has_any_refresh_in_progress(self) -> bool: ...

    def list_stale_refreshing(self, cutoff: datetime) -> Sequence[Position]: ...


class SqlAlchemyPositionRepository:
    """SQLAlchemy-backed Position repository."""

    def __init__(self, session: Session, user_id: str | None = None) -> None:
        self._session = session
        self._user_id = user_id

    def _base_query(self):
        q = self._session.query(Position)
        if self._user_id is not None:
            q = q.filter(Position.user_id == self._user_id)
        return q

    def list_all(self) -> Sequence[Position]:
        return self._base_query().all()

    def list_all_ids(self) -> list[int]:
        q = self._session.query(Position.id)
        if self._user_id is not None:
            q = q.filter(Position.user_id == self._user_id)
        return [pid for (pid,) in q.all()]

    def get_by_id(self, position_id: int) -> Optional[Position]:
        return self._base_query().filter(Position.id == position_id).first()

    def get_by_ids(self, position_ids: list[int]) -> Sequence[Position]:
        return self._base_query().filter(Position.id.in_(position_ids)).all()

    def add(self, position: Position) -> None:
        self._session.add(position)

    def delete(self, position: Position) -> None:
        self._session.delete(position)

    def refresh_instance(self, position: Position) -> None:
        self._session.refresh(position)

    def has_any_refresh_in_progress(self) -> bool:
        return (
            self._base_query()
            .filter(Position.refresh_in_progress.is_(True))
            .first()
        ) is not None

    def list_stale_refreshing(self, cutoff: datetime) -> Sequence[Position]:
        return (
            self._base_query()
            .filter(Position.refresh_in_progress.is_(True))
            .filter(Position.refresh_started_at.is_not(None))
            .filter(Position.refresh_started_at < cutoff)
            .all()
        )


# ---------------------------------------------------------------------------
# Key-level repository
# ---------------------------------------------------------------------------


class KeyLevelRepository(Protocol):
    """Data-access contract for PositionKeyLevel entities."""

    def get_by_position_and_id(
        self, position_id: int, level_id: int,
    ) -> Optional[PositionKeyLevel]: ...

    def add(self, key_level: PositionKeyLevel) -> None: ...

    def delete(self, key_level: PositionKeyLevel) -> None: ...


class SqlAlchemyKeyLevelRepository:
    """SQLAlchemy-backed user-scoped KeyLevel repository."""

    def __init__(self, session: Session, user_id: str | None = None) -> None:
        self._session = session
        self._user_id = user_id

    def get_by_position_and_id(
        self, position_id: int, level_id: int,
    ) -> Optional[PositionKeyLevel]:
        if self._user_id is None:
            logger.warning("Blocked key-level lookup without user context")
            return None
        query = (
            self._session.query(PositionKeyLevel)
            .filter(PositionKeyLevel.id == level_id)
            .filter(PositionKeyLevel.position_id == position_id)
        )
        query = query.join(Position).filter(Position.user_id == self._user_id)
        return query.first()

    def add(self, key_level: PositionKeyLevel) -> None:
        self._session.add(key_level)

    def delete(self, key_level: PositionKeyLevel) -> None:
        self._session.delete(key_level)


# ---------------------------------------------------------------------------
# User repository
# ---------------------------------------------------------------------------


class UserRepository(Protocol):
    """Data-access contract for User entities."""

    def get_by_id(self, user_id: str) -> Optional[User]: ...

    def add(self, user: User) -> None: ...


class SqlAlchemyUserRepository:
    """SQLAlchemy-backed User repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, user_id: str) -> Optional[User]:
        return self._session.query(User).filter(User.id == user_id).first()

    def add(self, user: User) -> None:
        self._session.add(user)


# ---------------------------------------------------------------------------
# Rule config repository
# ---------------------------------------------------------------------------


class RuleConfigRepository(Protocol):
    """Data-access contract for StrategyRuleConfig entities."""

    def list_by_investment_type(
        self, investment_type: str,
    ) -> Sequence[StrategyRuleConfig]: ...

    def list_enabled_by_investment_type(
        self, investment_type: str,
    ) -> Sequence[StrategyRuleConfig]: ...

    def list_enabled_by_investment_type_and_keys(
        self, investment_type: str, rule_keys: Sequence[str],
    ) -> Sequence[StrategyRuleConfig]: ...

    def get_by_investment_type_and_key(
        self, investment_type: str, rule_key: str,
    ) -> Optional[StrategyRuleConfig]: ...

    def list_by_key(self, rule_key: str) -> Sequence[StrategyRuleConfig]: ...

    def add(self, config: StrategyRuleConfig) -> None: ...

    def delete(self, config: StrategyRuleConfig) -> None: ...


class SqlAlchemyRuleConfigRepository:
    """SQLAlchemy-backed rule config repository."""

    def __init__(self, session: Session, user_id: str | None = None) -> None:
        self._session = session
        self._user_id = user_id

    def _base_query(self):
        q = self._session.query(StrategyRuleConfig)
        if self._user_id is not None:
            q = q.filter(StrategyRuleConfig.user_id == self._user_id)
        return q

    def list_by_investment_type(
        self, investment_type: str,
    ) -> Sequence[StrategyRuleConfig]:
        return (
            self._base_query()
            .filter(StrategyRuleConfig.investment_type == investment_type)
            .all()
        )

    def list_enabled_by_investment_type(
        self, investment_type: str,
    ) -> Sequence[StrategyRuleConfig]:
        return (
            self._base_query()
            .filter(StrategyRuleConfig.investment_type == investment_type)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .all()
        )

    def list_enabled_by_investment_type_and_keys(
        self, investment_type: str, rule_keys: Sequence[str],
    ) -> Sequence[StrategyRuleConfig]:
        return (
            self._base_query()
            .filter(StrategyRuleConfig.investment_type == investment_type)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .filter(StrategyRuleConfig.rule_key.in_(rule_keys))
            .all()
        )

    def get_by_investment_type_and_key(
        self, investment_type: str, rule_key: str,
    ) -> Optional[StrategyRuleConfig]:
        return (
            self._base_query()
            .filter(StrategyRuleConfig.investment_type == investment_type)
            .filter(StrategyRuleConfig.rule_key == rule_key)
            .first()
        )

    def list_by_key(self, rule_key: str) -> Sequence[StrategyRuleConfig]:
        return (
            self._base_query()
            .filter(StrategyRuleConfig.rule_key == rule_key)
            .all()
        )

    def add(self, config: StrategyRuleConfig) -> None:
        self._session.add(config)

    def delete(self, config: StrategyRuleConfig) -> None:
        self._session.delete(config)
