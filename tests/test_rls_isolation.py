"""Tests for database-backed user isolation plumbing."""

from __future__ import annotations

import importlib.util
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Position,
    PositionKeyLevel,
    PositionTheme,
    StrategyRuleConfig,
    Theme,
    User,
)
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


_SET_CONFIG_SQL = "SELECT set_config('app.current_user_id', :user_id, false)"


def test_authenticated_uow_sets_rls_user_id_for_postgres_sessions():
    session = _FakePostgresSession()

    uow = SqlAlchemyUnitOfWork(session, user_id="user-a")
    uow.commit()
    uow.rollback()

    assert session.commit_count == 1
    assert session.rollback_count == 1
    # session-scoped (is_local=False) so the GUC persists on the physical
    # connection across transactions.
    assert session.executed == [
        (_SET_CONFIG_SQL, {"user_id": "user-a"}),
        (_SET_CONFIG_SQL, {"user_id": "user-a"}),
        (_SET_CONFIG_SQL, {"user_id": "user-a"}),
    ]


def test_anonymous_uow_resets_rls_guc_to_sentinel():
    """Anonymous UoWs must explicitly reset the GUC to prevent RLS leakage.

    When a pooled connection previously served user-a and is then reused for
    an anonymous request, the session-scoped GUC would still carry user-a's
    identity unless we actively reset it.
    """
    session = _FakePostgresSession()

    uow = SqlAlchemyUnitOfWork(session, user_id=None)
    uow.commit()

    assert any(
        stmt == _SET_CONFIG_SQL and params == {"user_id": "__anonymous__"}
        for stmt, params in session.executed
    ), "Expected __anonymous__ sentinel to be set for anonymous sessions"


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
        theme_a = Theme(user_id="user-a", name="AI")
        theme_b = Theme(user_id="user-b", name="Energy")
        session.add_all([theme_a, theme_b])
        session.flush()
        session.add_all(
            [
                PositionTheme(position_id=position_a.id, theme_id=theme_a.id),
                PositionTheme(position_id=position_b.id, theme_id=theme_b.id),
            ]
        )
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
        assert [theme.id for theme in uow.themes.list_themes()] == [theme_a.id]
        assert uow.themes.get_by_id(theme_b.id) is None
        grouped = uow.themes.list_positions_grouped_by_theme()
        assert [
            (theme.name if theme else None, [position.id for position in positions])
            for theme, positions in grouped
        ] == [("AI", [position_a.id])]
    finally:
        session.close()
        engine.dispose()


def test_rls_policy_migration_creates_user_isolation_policies(monkeypatch):
    migration = _load_migration("b8f3a9c2d4e1_add_user_isolation_rls_policies.py")
    fake_op = _FakeMigrationOp("postgresql")
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    normalized_sql = " ".join("\n".join(fake_op.executed).split())
    assert "CREATE POLICY positions_isolation ON positions" in normalized_sql
    assert "CREATE POLICY strategy_rule_configs_isolation ON strategy_rule_configs" in normalized_sql
    assert "CREATE POLICY position_key_levels_isolation ON position_key_levels" in normalized_sql
    assert "user_id = NULLIF(current_setting('app.current_user_id', true), '')" in normalized_sql
    assert "FROM positions WHERE positions.id = position_key_levels.position_id" in normalized_sql


def test_theme_rls_policy_migration_creates_user_isolation_policies(monkeypatch):
    migration = _load_migration("f5a6b7c8d9e0_add_themes.py")
    fake_op = _FakeMigrationOp("postgresql")
    monkeypatch.setattr(migration, "op", fake_op)

    migration.upgrade()

    normalized_sql = " ".join("\n".join(fake_op.executed).split())
    assert "ALTER TABLE themes ENABLE ROW LEVEL SECURITY" in normalized_sql
    assert "ALTER TABLE position_themes ENABLE ROW LEVEL SECURITY" in normalized_sql
    assert "CREATE POLICY themes_isolation ON themes" in normalized_sql
    assert "CREATE POLICY position_themes_isolation ON position_themes" in normalized_sql
    assert "themes.user_id = NULLIF(current_setting('app.current_user_id', true), '')" in normalized_sql
    assert "positions.user_id = NULLIF(current_setting('app.current_user_id', true), '')" in normalized_sql
    assert "WHERE positions.id = position_themes.position_id" in normalized_sql
    assert "WHERE themes.id = position_themes.theme_id" in normalized_sql


def _load_migration(filename: str):
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / filename
    )
    spec = importlib.util.spec_from_file_location(Path(filename).stem, path)
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

    def create_table(self, *args, **kwargs):
        """No-op DDL stub; policy tests only inspect generated RLS SQL."""
        return None

    def create_index(self, *args, **kwargs):
        """No-op DDL stub; policy tests only inspect generated RLS SQL."""
        return None
