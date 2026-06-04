"""Tests for trim acknowledgement routes and portfolio overrides."""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_authenticated_uow, get_optional_uow, get_uow
from app.main import _enrich_position, app
from app.models import Base, Position, StrategyRuleConfig, User
from app.schemas import Verdict
from app.unit_of_work import SqlAlchemyUnitOfWork
from tests.csrf_utils import csrf_form_data


@pytest.fixture(autouse=True)
def _setup_db():
    """Use an in-memory SQLite database for each test."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    @event.listens_for(TestingSession.class_, "before_flush")
    def _assign_test_user_id(session, _flush_context, _instances):
        for obj in session.new:
            if isinstance(obj, User) and not obj.id:
                obj.id = "test-user-id"
            if isinstance(obj, Position) and obj.user_id is None:
                obj.user_id = "test-user-id"
            if isinstance(obj, StrategyRuleConfig) and obj.user_id is None:
                obj.user_id = "test-user-id"

    db = TestingSession()
    db.add(
        User(
            id="test-user-id",
            email="test@example.com",
            display_name="Test User",
            created_at=datetime.now(),
        )
    )
    db.commit()
    db.close()

    def override_get_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session)
        finally:
            session.close()

    def override_get_authenticated_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session, user_id="test-user-id")
        finally:
            session.close()

    def override_get_optional_uow():
        session = TestingSession()
        try:
            yield SqlAlchemyUnitOfWork(session, user_id="test-user-id")
        finally:
            session.close()

    app.dependency_overrides[get_uow] = override_get_uow
    app.dependency_overrides[get_authenticated_uow] = override_get_authenticated_uow
    app.dependency_overrides[get_optional_uow] = override_get_optional_uow
    yield TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app)


class TestTrimAcknowledgementLogic:
    def test_enrich_position_overrides_trim_to_hold_when_acknowledged(self):
        pos = Position(
            ticker="AAPL",
            company_name="Apple Inc.",
            cost_basis=100.0,
            initial_purchase_date=date(2025, 1, 1),
            investment_type="long-term",
            current_price=115.0,
            trim_acknowledged=True,
        )

        enriched = _enrich_position(pos)

        assert enriched["computed_verdict"] == Verdict.trim
        assert enriched["verdict"] == Verdict.hold
        assert enriched["trim_acknowledged"] is True
        trim_rule_desc = "Price is 15.0% above cost basis (>10%)"
        assert any(r.description == trim_rule_desc for r in enriched["triggered_rules"])
        assert trim_rule_desc in enriched["reason_sort_value"]

    def test_enrich_position_does_not_suppress_sell_when_trim_is_acknowledged(self):
        pos = Position(
            ticker="AAPL",
            company_name="Apple Inc.",
            cost_basis=100.0,
            initial_purchase_date=date(2025, 1, 1),
            investment_type="short-term",
            current_price=115.0,
            trim_acknowledged=True,
        )

        enriched = _enrich_position(pos, indicator_cache={("daily", 21): (90.0, 100.0)})

        assert enriched["computed_verdict"] == Verdict.sell
        assert enriched["verdict"] == Verdict.sell
        assert enriched["triggered_rules"][0].verdict == Verdict.sell
        assert enriched["triggered_rules"][0].description == "Daily close (90.00) < SMA-21 (100.00)"


class TestTrimAcknowledgementRoutes:
    def test_acknowledge_route_sets_flag_and_redirects(self, client, _setup_db):
        db = _setup_db()
        try:
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=115.0,
            )
            db.add(pos)
            db.commit()
            position_id = pos.id
        finally:
            db.close()

        resp = client.post(
            f"/trim-acknowledge/{position_id}",
            data=csrf_form_data(client),
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        verify_db = _setup_db()
        try:
            refreshed = verify_db.query(Position).filter(Position.id == position_id).first()
            assert refreshed is not None
            assert refreshed.trim_acknowledged is True
        finally:
            verify_db.close()

    def test_unacknowledge_route_clears_flag_and_redirects(self, client, _setup_db):
        db = _setup_db()
        try:
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2025, 1, 1),
                investment_type="long-term",
                current_price=115.0,
                trim_acknowledged=True,
            )
            db.add(pos)
            db.commit()
            position_id = pos.id
        finally:
            db.close()

        resp = client.post(
            f"/trim-unacknowledge/{position_id}",
            data=csrf_form_data(client),
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        verify_db = _setup_db()
        try:
            refreshed = verify_db.query(Position).filter(Position.id == position_id).first()
            assert refreshed is not None
            assert refreshed.trim_acknowledged is False
        finally:
            verify_db.close()


class TestTrimAcknowledgementPortfolioUi:
    def test_portfolio_shows_mark_as_trimmed_only_for_trim_verdicts(self, client, _setup_db):
        db = _setup_db()
        try:
            db.add_all(
                [
                    Position(
                        ticker="AAPL",
                        company_name="Apple Inc.",
                        cost_basis=100.0,
                        initial_purchase_date=date(2025, 1, 1),
                        investment_type="long-term",
                        current_price=115.0,
                    ),
                    Position(
                        ticker="MSFT",
                        company_name="Microsoft Corp.",
                        cost_basis=100.0,
                        initial_purchase_date=date(2025, 1, 1),
                        investment_type="long-term",
                        current_price=100.0,
                    ),
                ]
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/")

        assert resp.status_code == 200
        assert resp.text.count("Mark as Trimmed") == 1
        assert "Clear Trim" not in resp.text

    def test_portfolio_shows_trimmed_indicator_clear_button_and_original_trim_rule(
        self, client, _setup_db
    ):
        db = _setup_db()
        try:
            db.add(
                Position(
                    ticker="AAPL",
                    company_name="Apple Inc.",
                    cost_basis=100.0,
                    initial_purchase_date=date(2025, 1, 1),
                    investment_type="long-term",
                    current_price=115.0,
                    trim_acknowledged=True,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/")

        assert resp.status_code == 200
        assert "Hold" in resp.text
        assert "Trimmed ✓" in resp.text
        assert "Clear Trim" in resp.text
        assert "Trim acknowledged" not in resp.text
        assert "Price is 15.0% above cost basis (&gt;10%)" in resp.text

    def test_portfolio_does_not_show_trimmed_indicator_for_natural_hold(self, client, _setup_db):
        db = _setup_db()
        try:
            db.add(
                Position(
                    ticker="AAPL",
                    company_name="Apple Inc.",
                    cost_basis=100.0,
                    initial_purchase_date=date(2025, 1, 1),
                    investment_type="long-term",
                    current_price=105.0,
                    trim_acknowledged=True,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.get("/")

        assert resp.status_code == 200
        assert "Hold" in resp.text
        assert "Clear Trim" in resp.text
        assert "Trimmed ✓" not in resp.text
