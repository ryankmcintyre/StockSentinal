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
    Background tasks that operate across all users should omit ``user_id``.
    """

    def __init__(self, session: Session, user_id: str | None = None) -> None:
        self._session = session
        self.positions = SqlAlchemyPositionRepository(session, user_id)
        self.key_levels = SqlAlchemyKeyLevelRepository(session)
        self.rule_configs = SqlAlchemyRuleConfigRepository(session, user_id)
        self.users = SqlAlchemyUserRepository(session)

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


def as_uow(session: Session) -> SqlAlchemyUnitOfWork:
    """Wrap an existing SQLAlchemy session in the default unit-of-work."""
    return SqlAlchemyUnitOfWork(session)
