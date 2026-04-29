"""Repository interfaces (Protocols) and SQLAlchemy implementations.

This module isolates all data-access queries behind repository abstractions
so that the application layer (routes, services) never imports or calls
SQLAlchemy directly.  The Protocol classes define the contract; the
``SqlAlchemy*`` classes are the default implementations backed by a
SQLAlchemy ``Session``.

Market-data cache repositories live in ``app.market_data.cache_repos``
since they are tightly coupled to the market data service.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol, Sequence

from sqlalchemy.orm import Session

from app.models import (
    Position,
    PositionKeyLevel,
    StrategyRuleConfig,
)


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

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_all(self) -> Sequence[Position]:
        return self._session.query(Position).all()

    def list_all_ids(self) -> list[int]:
        return [pid for (pid,) in self._session.query(Position.id).all()]

    def get_by_id(self, position_id: int) -> Optional[Position]:
        return self._session.query(Position).filter(Position.id == position_id).first()

    def get_by_ids(self, position_ids: list[int]) -> Sequence[Position]:
        return self._session.query(Position).filter(Position.id.in_(position_ids)).all()

    def add(self, position: Position) -> None:
        self._session.add(position)

    def delete(self, position: Position) -> None:
        self._session.delete(position)

    def refresh_instance(self, position: Position) -> None:
        self._session.refresh(position)

    def has_any_refresh_in_progress(self) -> bool:
        return (
            self._session.query(Position)
            .filter(Position.refresh_in_progress.is_(True))
            .first()
        ) is not None

    def list_stale_refreshing(self, cutoff: datetime) -> Sequence[Position]:
        return (
            self._session.query(Position)
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
    """SQLAlchemy-backed KeyLevel repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_position_and_id(
        self, position_id: int, level_id: int,
    ) -> Optional[PositionKeyLevel]:
        return (
            self._session.query(PositionKeyLevel)
            .filter(PositionKeyLevel.id == level_id)
            .filter(PositionKeyLevel.position_id == position_id)
            .first()
        )

    def add(self, key_level: PositionKeyLevel) -> None:
        self._session.add(key_level)

    def delete(self, key_level: PositionKeyLevel) -> None:
        self._session.delete(key_level)


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

    def __init__(self, session: Session) -> None:
        self._session = session

    def list_by_investment_type(
        self, investment_type: str,
    ) -> Sequence[StrategyRuleConfig]:
        return (
            self._session.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type)
            .all()
        )

    def list_enabled_by_investment_type(
        self, investment_type: str,
    ) -> Sequence[StrategyRuleConfig]:
        return (
            self._session.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .all()
        )

    def list_enabled_by_investment_type_and_keys(
        self, investment_type: str, rule_keys: Sequence[str],
    ) -> Sequence[StrategyRuleConfig]:
        return (
            self._session.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .filter(StrategyRuleConfig.rule_key.in_(rule_keys))
            .all()
        )

    def get_by_investment_type_and_key(
        self, investment_type: str, rule_key: str,
    ) -> Optional[StrategyRuleConfig]:
        return (
            self._session.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type)
            .filter(StrategyRuleConfig.rule_key == rule_key)
            .first()
        )

    def list_by_key(self, rule_key: str) -> Sequence[StrategyRuleConfig]:
        return (
            self._session.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.rule_key == rule_key)
            .all()
        )

    def add(self, config: StrategyRuleConfig) -> None:
        self._session.add(config)

    def delete(self, config: StrategyRuleConfig) -> None:
        self._session.delete(config)
