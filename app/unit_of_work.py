"""Unit-of-work abstraction for transactional data access.

The ``UnitOfWork`` protocol defines the contract: a context manager that
exposes repository accessors and ``commit`` / ``rollback`` for explicit
transaction control.  ``SqlAlchemyUnitOfWork`` is the default
implementation backed by a SQLAlchemy ``Session``.
"""

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
)


class UnitOfWork(Protocol):
    """Transactional boundary that provides access to all repositories."""

    positions: PositionRepository
    key_levels: KeyLevelRepository
    rule_configs: RuleConfigRepository

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type, exc_val, exc_tb) -> None: ...


class SqlAlchemyUnitOfWork:
    """SQLAlchemy-backed unit of work.

    When used as a context manager, the session is automatically closed
    on exit.  The caller is responsible for calling ``commit()``
    explicitly — an uncommitted session is rolled back on close.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self.positions = SqlAlchemyPositionRepository(session)
        self.key_levels = SqlAlchemyKeyLevelRepository(session)
        self.rule_configs = SqlAlchemyRuleConfigRepository(session)

    @property
    def session(self) -> Session:
        """Expose the underlying session for edge cases (e.g. init_db).

        New code should prefer repository methods over direct session access.
        """
        return self._session

    def commit(self) -> None:
        self._session.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._session.close()
