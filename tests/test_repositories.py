from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Position, StrategyRuleConfig, User
from app.repositories import SqlAlchemyPositionRepository, SqlAlchemyRuleConfigRepository
from app.unit_of_work import SqlAlchemyUnitOfWork


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_user_owned_model_columns_are_not_nullable():
    assert Position.__table__.c.user_id.nullable is False
    assert StrategyRuleConfig.__table__.c.user_id.nullable is False


def test_user_scoped_repository_constructors_require_user_id(session):
    with pytest.raises(TypeError):
        SqlAlchemyPositionRepository(session)

    with pytest.raises(TypeError):
        SqlAlchemyRuleConfigRepository(session)


def test_user_scoped_repository_constructors_reject_none_user_id(session):
    with pytest.raises(ValueError, match="user_id is required"):
        SqlAlchemyPositionRepository(session, user_id=None)

    with pytest.raises(ValueError, match="user_id is required"):
        SqlAlchemyRuleConfigRepository(session, user_id=None)


def test_unscoped_unit_of_work_allows_users_but_rejects_scoped_repositories(session):
    uow = SqlAlchemyUnitOfWork(session)

    assert uow.users is not None

    with pytest.raises(ValueError, match="user_id is required"):
        _ = uow.positions

    with pytest.raises(ValueError, match="user_id is required"):
        _ = uow.rule_configs


def test_position_repository_filters_by_required_user_id(session):
    session.add_all([
        User(id="user-1", created_at=datetime.now()),
        User(id="user-2", created_at=datetime.now()),
        Position(
            ticker="AAPL",
            company_name="Apple Inc.",
            cost_basis=100.0,
            initial_purchase_date=date(2026, 1, 1),
            investment_type="long-term",
            current_price=110.0,
            user_id="user-1",
        ),
        Position(
            ticker="MSFT",
            company_name="Microsoft",
            cost_basis=200.0,
            initial_purchase_date=date(2026, 1, 1),
            investment_type="long-term",
            current_price=210.0,
            user_id="user-2",
        ),
    ])
    session.commit()

    positions = SqlAlchemyPositionRepository(session, user_id="user-1").list_all()

    assert [position.ticker for position in positions] == ["AAPL"]


def test_rule_config_repository_filters_by_required_user_id(session):
    session.add_all([
        User(id="user-1", created_at=datetime.now()),
        User(id="user-2", created_at=datetime.now()),
        StrategyRuleConfig(
            user_id="user-1",
            investment_type="long-term",
            rule_key="RULE_A",
            enabled=True,
            sort_order=1,
        ),
        StrategyRuleConfig(
            user_id="user-2",
            investment_type="long-term",
            rule_key="RULE_B",
            enabled=True,
            sort_order=1,
        ),
    ])
    session.commit()

    configs = SqlAlchemyRuleConfigRepository(
        session, user_id="user-1"
    ).list_by_investment_type("long-term")

    assert [config.rule_key for config in configs] == ["RULE_A"]
