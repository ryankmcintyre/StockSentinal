"""Unit-of-work abstraction for transactional data access."""

from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import event, text
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

logger = logging.getLogger(__name__)


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
        self._is_postgresql = session.get_bind().dialect.name == "postgresql"
        self._uses_transaction_hook = False
        self._install_current_user_id_hook()
        self._ensure_current_user_id()
        self._positions = (
            SqlAlchemyPositionRepository(session, user_id)
            if user_id is not None
            else None
        )
        self.key_levels = SqlAlchemyKeyLevelRepository(session, user_id)
        self._rule_configs = (
            SqlAlchemyRuleConfigRepository(session, user_id)
            if user_id is not None
            else None
        )
        self.users = SqlAlchemyUserRepository(session)

    @property
    def positions(self) -> PositionRepository:
        if self._positions is None:
            raise ValueError(
                "Cannot access positions without user_id. "
                "Create UnitOfWork with user_id parameter to access user-scoped repositories"
            )
        return self._positions

    @property
    def rule_configs(self) -> RuleConfigRepository:
        if self._rule_configs is None:
            raise ValueError(
                "Cannot access rule_configs without user_id. "
                "Create UnitOfWork with user_id parameter to access user-scoped repositories"
            )
        return self._rule_configs

    @property
    def session(self) -> Session:
        return self._session

    def _set_current_user_id(self) -> None:
        if self.user_id is None or not self._is_postgresql:
            return

        self._session.execute(
            text("SELECT set_config('app.current_user_id', :user_id, true)"),
            {"user_id": self.user_id},
        )

    def _install_current_user_id_hook(self) -> None:
        if self.user_id is None or not self._is_postgresql:
            return
        if not hasattr(self._session, "dispatch"):
            logger.debug(
                "Skipping app.current_user_id transaction hook because session has no dispatch"
            )
            return

        user_id = self.user_id
        assert user_id is not None

        @event.listens_for(self._session, "after_begin")
        def _set_current_user_id_on_begin(_session, _transaction, connection):
            connection.execute(
                text("SELECT set_config('app.current_user_id', :user_id, true)"),
                {"user_id": user_id},
            )

        self._uses_transaction_hook = True

    def _ensure_current_user_id(self) -> None:
        if self._uses_transaction_hook:
            return
        self._set_current_user_id()

    def commit(self) -> None:
        self._session.commit()
        self._ensure_current_user_id()

    def rollback(self) -> None:
        self._session.rollback()
        self._ensure_current_user_id()

    def __enter__(self) -> "SqlAlchemyUnitOfWork":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._session.close()


def as_uow(session: Session, user_id: str | None = None) -> SqlAlchemyUnitOfWork:
    """Wrap an existing SQLAlchemy session in the default unit-of-work."""
    return SqlAlchemyUnitOfWork(session, user_id=user_id)
