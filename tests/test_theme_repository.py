from datetime import date, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, Position, PositionTheme, Theme, User
from app.repositories import SqlAlchemyThemeRepository, ThemeNameConflictError


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


def _add_user(session, user_id: str):
    session.add(User(id=user_id, email=f"{user_id}@example.com", created_at=datetime.now()))
    session.commit()


def _add_position(session, user_id: str, ticker: str = "AAPL") -> Position:
    position = Position(
        ticker=ticker,
        company_name=f"{ticker} Corp",
        cost_basis=100.0,
        initial_purchase_date=date(2025, 1, 1),
        investment_type="long-term",
        current_price=125.0,
        user_id=user_id,
    )
    session.add(position)
    session.commit()
    return position


def test_theme_names_are_case_insensitive_unique_per_user(session):
    _add_user(session, "user-1")
    repo = SqlAlchemyThemeRepository(session, "user-1")

    ai = repo.create_theme("AI")
    energy = repo.create_theme("Energy")
    session.commit()

    assert [ai.name, energy.name] == ["AI", "Energy"]
    with pytest.raises(ThemeNameConflictError):
        repo.create_theme(" ai ")


def test_same_theme_name_allowed_for_different_users(session):
    _add_user(session, "user-1")
    _add_user(session, "user-2")

    SqlAlchemyThemeRepository(session, "user-1").create_theme("AI")
    SqlAlchemyThemeRepository(session, "user-2").create_theme(" ai ")
    session.commit()

    assert session.query(Theme).count() == 2


def test_position_can_have_multiple_user_scoped_themes(session):
    _add_user(session, "user-1")
    position = _add_position(session, "user-1")
    repo = SqlAlchemyThemeRepository(session, "user-1")
    ai = repo.create_theme("AI")
    growth = repo.create_theme("Growth")
    session.commit()

    repo.set_position_themes(position.id, [ai.id, growth.id])
    session.commit()

    session.refresh(position)
    assert sorted(theme.name for theme in position.themes) == ["AI", "Growth"]


def test_cross_user_theme_assignment_is_rejected(session):
    _add_user(session, "user-1")
    _add_user(session, "user-2")
    position = _add_position(session, "user-1")
    other_theme = SqlAlchemyThemeRepository(session, "user-2").create_theme("Energy")
    session.commit()

    with pytest.raises(ValueError, match="do not belong"):
        SqlAlchemyThemeRepository(session, "user-1").set_position_themes(
            position.id,
            [other_theme.id],
        )


def test_list_positions_grouped_by_theme_includes_untagged_bucket(session):
    _add_user(session, "user-1")
    tagged = _add_position(session, "user-1", "NVDA")
    untagged = _add_position(session, "user-1", "MSFT")
    repo = SqlAlchemyThemeRepository(session, "user-1")
    ai = repo.create_theme("AI")
    empty = repo.create_theme("Energy")
    session.commit()
    repo.set_position_themes(tagged.id, [ai.id])
    session.commit()

    grouped = repo.list_positions_grouped_by_theme()

    assert [(theme.name if theme else None, [p.ticker for p in positions]) for theme, positions in grouped] == [
        ("AI", ["NVDA"]),
        ("Energy", []),
        (None, ["MSFT"]),
    ]
    assert empty.name == "Energy"
    assert untagged.ticker == "MSFT"


def test_deleting_position_removes_theme_associations(session):
    _add_user(session, "user-1")
    position = _add_position(session, "user-1")
    repo = SqlAlchemyThemeRepository(session, "user-1")
    theme = repo.create_theme("AI")
    session.commit()
    repo.set_position_themes(position.id, [theme.id])
    session.commit()

    session.delete(position)
    session.commit()

    assert session.query(PositionTheme).count() == 0
    assert session.query(Theme).filter(Theme.id == theme.id).one()


def test_deleting_theme_removes_associations_but_not_positions(session):
    _add_user(session, "user-1")
    position = _add_position(session, "user-1")
    repo = SqlAlchemyThemeRepository(session, "user-1")
    theme = repo.create_theme("AI")
    session.commit()
    repo.set_position_themes(position.id, [theme.id])
    session.commit()

    assert repo.delete_theme(theme.id) is True
    session.commit()

    assert session.query(PositionTheme).count() == 0
    assert session.query(Position).filter(Position.id == position.id).one()
