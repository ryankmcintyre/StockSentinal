"""Tests for database-backed user isolation plumbing."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Position, PositionKeyLevel, StrategyRuleConfig, User
from app.unit_of_work import SqlAlchemyUnitOfWork


class _FakePostgresSession:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, str]]] = []
        self.commit_count = 0
        self.rollback_count = 0
        self._bind = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql")
        )

    def get_bind(self):
        return self._bind

    def execute(self, statement, params):
        self.executed.append((str(statement), params))

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1


def test_authenticated_uow_sets_rls_user_id_for_postgres_sessions():
    session = _FakePostgresSession()

    uow = SqlAlchemyUnitOfWork(session, user_id="user-a")
    uow.commit()
    uow.rollback()

    assert session.commit_count == 1
    assert session.rollback_count == 1
    assert session.executed == [
        ("SELECT set_config('app.current_user_id', :user_id, true)", {"user_id": "user-a"}),
        ("SELECT set_config('app.current_user_id', :user_id, true)", {"user_id": "user-a"}),
        ("SELECT set_config('app.current_user_id', :user_id, true)", {"user_id": "user-a"}),
    ]


def test_user_scoped_repositories_hide_cross_user_rows():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SessionMaker = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    session = SessionMaker()
    try:
        user_a = User(id="user-a", email="a@example.com", created_at=datetime.now())
        user_b = User(id="user-b", email="b@example.com", created_at=datetime.now())
        position_a = Position(
            ticker="AAPL",
            company_name="Apple Inc.",
            cost_basis=100.0,
            initial_purchase_date=date(2024, 1, 1),
            investment_type="long-term",
            current_price=120.0,
            user_id="user-a",
        )
        position_b = Position(
            ticker="MSFT",
            company_name="Microsoft",
            cost_basis=100.0,
            initial_purchase_date=date(2024, 1, 1),
            investment_type="long-term",
            current_price=120.0,
            user_id="user-b",
        )
        session.add_all([user_a, user_b, position_a, position_b])
        session.flush()

        level_a = PositionKeyLevel(position_id=position_a.id, level_price=110.0)
        level_b = PositionKeyLevel(position_id=position_b.id, level_price=110.0)
        rule_a = StrategyRuleConfig(
            user_id="user-a",
            investment_type="long-term",
            rule_key="trim_gain",
            enabled=True,
        )
        rule_b = StrategyRuleConfig(
            user_id="user-b",
            investment_type="long-term",
            rule_key="trim_gain",
            enabled=True,
        )
        session.add_all([level_a, level_b, rule_a, rule_b])
        session.commit()

        uow = SqlAlchemyUnitOfWork(session, user_id="user-a")

        assert [position.id for position in uow.positions.list_all()] == [position_a.id]
        assert uow.positions.get_by_id(position_b.id) is None
        assert [
            config.user_id for config in uow.rule_configs.list_by_key("trim_gain")
        ] == ["user-a"]
        assert (
            uow.key_levels.get_by_position_and_id(position_a.id, level_a.id).id
            == level_a.id
        )
        assert uow.key_levels.get_by_position_and_id(position_b.id, level_b.id) is None
    finally:
        session.close()
        engine.dispose()


def test_rls_policy_migration_creates_user_isolation_policies(monkeypatch):
    migration = _load_policy_migration()
    fake_op = _FakeMigrationOp("postgresql")
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    normalized_sql = " ".join("\n".join(fake_op.executed).split())
    assert "CREATE POLICY positions_isolation ON positions" in normalized_sql
    assert "CREATE POLICY strategy_rule_configs_isolation ON strategy_rule_configs" in normalized_sql
    assert "CREATE POLICY position_key_levels_isolation ON position_key_levels" in normalized_sql
    assert "user_id = NULLIF(current_setting('app.current_user_id', true), '')" in normalized_sql
    assert "FROM positions WHERE positions.id = position_key_levels.position_id" in normalized_sql


def _load_policy_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "b8f3a9c2d4e1_add_user_isolation_rls_policies.py"
    )
    spec = importlib.util.spec_from_file_location("rls_policy_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakeMigrationOp:
    def __init__(self, dialect_name: str) -> None:
        self.executed: list[str] = []
        self._bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect_name))

    def get_bind(self):
        return self._bind

    def execute(self, sql: str):
        self.executed.append(sql)
