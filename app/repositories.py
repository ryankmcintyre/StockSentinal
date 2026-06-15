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

from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Position,
    PositionKeyLevel,
    StrategyRuleConfig,
    Theme,
    User,
)

logger = logging.getLogger(__name__)


class ThemeNameConflictError(ValueError):
    """Raised when a user attempts to create or rename a theme to a duplicate name."""


def _require_user_id(user_id: str | None) -> str:
    """Validate user-scoped repositories are never constructed without a user."""
    if user_id is None:
        raise ValueError("user_id is required for user-scoped repository construction")
    return user_id


# ---------------------------------------------------------------------------
# Position repository
# ---------------------------------------------------------------------------


class PositionRepository(Protocol):
    """Data-access contract for Position entities."""

    def list_all(self) -> Sequence[Position]: ...

    def list_all_ids(self) -> list[int]: ...

    def count_all(self) -> int: ...

    def get_by_id(self, position_id: int) -> Optional[Position]: ...

    def get_by_ids(self, position_ids: list[int]) -> Sequence[Position]: ...

    def add(self, position: Position) -> None: ...

    def delete(self, position: Position) -> None: ...

    def refresh_instance(self, position: Position) -> None:
        """Re-read the instance from the database (e.g. to get auto-generated id)."""
        ...

    def has_any_refresh_in_progress(self) -> bool: ...

    def list_refresh_statuses(self) -> Sequence[tuple[int, bool | None, datetime | None]]:
        """Return tuples of position_id, refresh_in_progress, and refresh_started_at."""
        ...

    def clear_stale_refreshing(self, cutoff: datetime) -> int:
        """Bulk-clear refresh flags started before cutoff and return updated row count."""
        ...

    def list_stale_refreshing(self, cutoff: datetime) -> Sequence[Position]: ...


class SqlAlchemyPositionRepository:
    """SQLAlchemy-backed Position repository."""

    def __init__(self, session: Session, user_id: str) -> None:
        self._session = session
        self._user_id = _require_user_id(user_id)

    def _base_query(self):
        return self._session.query(Position).filter(Position.user_id == self._user_id)

    def list_all(self) -> Sequence[Position]:
        return self._base_query().all()

    def list_all_ids(self) -> list[int]:
        q = self._session.query(Position.id).filter(Position.user_id == self._user_id)
        return [pid for (pid,) in q.all()]

    def count_all(self) -> int:
        return self._base_query().count()

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
            .with_entities(Position.id)
            .filter(Position.refresh_in_progress.is_(True))
            .first()
        ) is not None

    def list_refresh_statuses(self) -> Sequence[tuple[int, bool | None, datetime | None]]:
        """Return lightweight refresh status rows for each position.

        Each tuple contains: position_id (int), refresh_in_progress (bool | None),
        refresh_started_at (datetime | None).
        """
        return (
            self._base_query()
            .with_entities(
                Position.id,
                Position.refresh_in_progress,
                Position.refresh_started_at,
            )
            .all()
        )

    def clear_stale_refreshing(self, cutoff: datetime) -> int:
        """Bulk-clear refresh flags started before cutoff and return updated row count."""
        return (
            self._base_query()
            .filter(Position.refresh_in_progress.is_(True))
            .filter(Position.refresh_started_at.is_not(None))
            .filter(Position.refresh_started_at < cutoff)
            .update(
                {
                    Position.refresh_in_progress: False,
                    Position.refresh_started_at: None,
                },
                synchronize_session=False,
            )
        )

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
            logger.warning(
                "Blocked key-level lookup without user context",
                extra={"position_id": position_id, "level_id": level_id},
            )
            raise ValueError("Cannot lookup key level without user context")
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
# Theme repository
# ---------------------------------------------------------------------------


class ThemeRepository(Protocol):
    """Data-access contract for user-defined Theme/Sector/Industry tags."""

    def list_themes(self) -> Sequence[Theme]: ...

    def get_by_id(self, theme_id: int) -> Optional[Theme]: ...

    def create_theme(self, name: str) -> Theme: ...

    def rename_theme(self, theme_id: int, name: str) -> Optional[Theme]: ...

    def delete_theme(self, theme_id: int) -> bool: ...

    def set_position_themes(self, position_id: int, theme_ids: Sequence[int]) -> None: ...

    def list_positions_grouped_by_theme(self) -> list[tuple[Theme | None, list[Position]]]: ...


class SqlAlchemyThemeRepository:
    """SQLAlchemy-backed user-scoped Theme repository."""

    def __init__(self, session: Session, user_id: str) -> None:
        self._session = session
        self._user_id = _require_user_id(user_id)

    @staticmethod
    def _clean_name(name: str) -> str:
        return " ".join(name.strip().split())

    def _base_query(self):
        return self._session.query(Theme).filter(Theme.user_id == self._user_id)

    def _find_by_name(self, name: str) -> Optional[Theme]:
        clean_name = self._clean_name(name)
        return (
            self._base_query()
            .filter(func.lower(Theme.name) == clean_name.lower())
            .first()
        )

    def list_themes(self) -> Sequence[Theme]:
        return self._base_query().order_by(func.lower(Theme.name), Theme.name).all()

    def get_by_id(self, theme_id: int) -> Optional[Theme]:
        return self._base_query().filter(Theme.id == theme_id).first()

    def create_theme(self, name: str) -> Theme:
        clean_name = self._clean_name(name)
        if not clean_name:
            raise ValueError("Theme name is required")
        if self._find_by_name(clean_name) is not None:
            raise ThemeNameConflictError("Theme name already exists")
        theme = Theme(name=clean_name, user_id=self._user_id)
        self._session.add(theme)
        self._session.flush()
        return theme

    def rename_theme(self, theme_id: int, name: str) -> Optional[Theme]:
        theme = self.get_by_id(theme_id)
        if theme is None:
            return None
        clean_name = self._clean_name(name)
        if not clean_name:
            raise ValueError("Theme name is required")
        existing = self._find_by_name(clean_name)
        if existing is not None and existing.id != theme.id:
            raise ThemeNameConflictError("Theme name already exists")
        theme.name = clean_name
        self._session.flush()
        return theme

    def delete_theme(self, theme_id: int) -> bool:
        theme = self.get_by_id(theme_id)
        if theme is None:
            return False
        self._session.delete(theme)
        self._session.flush()
        return True

    def set_position_themes(self, position_id: int, theme_ids: Sequence[int]) -> None:
        position = (
            self._session.query(Position)
            .options(selectinload(Position.themes))
            .filter(Position.user_id == self._user_id, Position.id == position_id)
            .first()
        )
        if position is None:
            raise ValueError("Position not found")

        unique_theme_ids = list(dict.fromkeys(int(theme_id) for theme_id in theme_ids))
        if not unique_theme_ids:
            position.themes = []
            self._session.flush()
            return

        themes = (
            self._base_query()
            .filter(Theme.id.in_(unique_theme_ids))
            .order_by(func.lower(Theme.name), Theme.name)
            .all()
        )
        found_ids = {theme.id for theme in themes}
        if found_ids != set(unique_theme_ids):
            raise ValueError("One or more themes do not belong to this user")
        position.themes = themes
        self._session.flush()

    def list_positions_grouped_by_theme(self) -> list[tuple[Theme | None, list[Position]]]:
        themes = list(self.list_themes())
        positions = (
            self._session.query(Position)
            .options(selectinload(Position.themes))
            .filter(Position.user_id == self._user_id)
            .order_by(Position.ticker, Position.company_name)
            .all()
        )
        positions_by_theme_id: dict[int, list[Position]] = {theme.id: [] for theme in themes}
        untagged: list[Position] = []
        for position in positions:
            user_themes = [theme for theme in position.themes if theme.user_id == self._user_id]
            if not user_themes:
                untagged.append(position)
                continue
            for theme in user_themes:
                positions_by_theme_id.setdefault(theme.id, []).append(position)

        grouped = [(theme, positions_by_theme_id.get(theme.id, [])) for theme in themes]
        if untagged:
            grouped.append((None, untagged))
        return grouped


# ---------------------------------------------------------------------------
# User repository
# ---------------------------------------------------------------------------


class UserRepository(Protocol):
    """Data-access contract for User entities."""

    def get_by_id(self, user_id: str) -> Optional[User]: ...

    def list_with_position_counts(self) -> Sequence[tuple[User, int]]: ...

    def count_admins(self, for_update: bool = False) -> int: ...

    def add(self, user: User) -> None: ...


class SqlAlchemyUserRepository:
    """SQLAlchemy-backed User repository."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_id(self, user_id: str) -> Optional[User]:
        return self._session.query(User).filter(User.id == user_id).first()

    def list_with_position_counts(self) -> Sequence[tuple[User, int]]:
        return (
            self._session.query(User, func.count(Position.id))
            .outerjoin(Position, Position.user_id == User.id)
            .group_by(User.id)
            .order_by(User.created_at.desc(), User.email)
            .all()
        )

    def count_admins(self, for_update: bool = False) -> int:
        query = self._session.query(User).filter(User.is_admin.is_(True))
        if for_update:
            return len(query.with_for_update().all())
        return query.count()

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

    def __init__(self, session: Session, user_id: str) -> None:
        self._session = session
        self._user_id = _require_user_id(user_id)

    def _base_query(self):
        return self._session.query(StrategyRuleConfig).filter(
            StrategyRuleConfig.user_id == self._user_id
        )

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
