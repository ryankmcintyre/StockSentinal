"""Unit-of-work abstraction for transactional data access."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.repositories import (
    KeyLevelRepository,
    PositionRepository,
    RuleConfigRepository,
    SqlAlchemyKeyLevelRepository,
    SqlAlchemyPositionRepository,
    SqlAlchemyRuleConfigRepository,
    SqlAlchemyUserRepository,
    UserRepository,
)


class UnitOfWork(Protocol):
    """Transactional boundary that provides access to all repositories."""

    positions: PositionRepository
    key_levels: KeyLevelRepository
    rule_configs: RuleConfigRepository
    users: UserRepository
    user_id: str | None

    @property
    def session(self) -> Session: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...


class SqlAlchemyUnitOfWork:
    """SQLAlchemy-backed unit of work.

    When used as a context manager, the session is automatically closed
    on exit.  The caller is responsible for calling ``commit()``
    explicitly — an uncommitted session is rolled back on close.

    Pass ``user_id`` to scope position and rule-config queries to a specific user.
    Omitting ``user_id`` is only appropriate for user-repository operations such
    as auth bootstrap flows.
    """

    def __init__(self, session: Session, user_id: str | None = None) -> None:
        self._session = session
        self.user_id = user_id
        self._positions = (
            SqlAlchemyPositionRepository(session, user_id)
            if user_id is not None
            else None
        )
        self.key_levels = SqlAlchemyKeyLevelRepository(session)
        self._rule_configs = (
            SqlAlchemyRuleConfigRepository(session, user_id)
            if user_id is not None
            else None
        )
        self.users = SqlAlchemyUserRepository(session)

    @property
    def positions(self) -> PositionRepository:
        if self._positions is None:
            raise ValueError("Cannot access positions: UnitOfWork requires user_id")
        return self._positions

    @property
    def rule_configs(self) -> RuleConfigRepository:
        if self._rule_configs is None:
            raise ValueError("Cannot access rule_configs: UnitOfWork requires user_id")
        return self._rule_configs

    @property
    def session(self) -> Session:
        return self._session

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._session.close()


def as_uow(session: Session, user_id: str | None = None) -> SqlAlchemyUnitOfWork:
    """Wrap an existing SQLAlchemy session in the default unit-of-work."""
    return SqlAlchemyUnitOfWork(session, user_id=user_id)
