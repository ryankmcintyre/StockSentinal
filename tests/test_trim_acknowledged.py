"""Tests for the "Mark as Trimmed" feature (issue #120).

Covers:
- _enrich_position: Trim→Hold override when trim_acknowledged is set
- _enrich_position: Sell not suppressed by trim_acknowledged
- _enrich_position: Trim shows normally when trim_acknowledged is False/None
- POST /trim-acknowledge/{id}: sets flag and redirects
- POST /trim-unacknowledge/{id}: clears flag and redirects
- Portfolio page: shows "Mark as Trimmed" button for Trim positions
- Portfolio page: shows "Clear Trim" button when acknowledged
- Portfolio page: shows "Trimmed ✓" note and "Trim acknowledged" tag when acknowledged
"""

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_authenticated_uow, get_optional_uow, get_uow
from app.main import _enrich_position, app
from app.models import Base, Position, StrategyRuleConfig, User
from app.schemas import InvestmentType, Verdict
from app.unit_of_work import SqlAlchemyUnitOfWork
from tests.csrf_utils import csrf_form_data


# ---------------------------------------------------------------------------
# DB fixture (mirrors test_refresh_route.py)
# ---------------------------------------------------------------------------


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
    db.add(User(id="test-user-id", email="test@example.com", display_name="Test User", created_at=datetime.now()))
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


def _make_position(**kwargs) -> Position:
    """Return an un-persisted Position with sensible defaults for rule-engine testing."""
    defaults = dict(
        ticker="AAPL",
        company_name="Apple Inc.",
        cost_basis=100.0,
        initial_purchase_date=date(2024, 1, 1),
        investment_type=InvestmentType.long_term,
        current_price=115.0,
        user_id="test-user-id",
    )
    defaults.update(kwargs)
    return Position(**defaults)


# ---------------------------------------------------------------------------
# Unit tests: _enrich_position override logic
# ---------------------------------------------------------------------------


class TestEnrichPositionTrimOverride:
    def test_trim_acknowledged_overrides_verdict_to_hold(self):
        """When trim_acknowledged=True and Trim fires (15% gain), verdict becomes Hold."""
        pos = _make_position(cost_basis=100.0, current_price=115.0, trim_acknowledged=True)
        result = _enrich_position(pos)
        assert result["verdict"] == Verdict.hold
        assert result["trim_acknowledged"] is True

    def test_trim_acknowledged_clears_triggered_rules(self):
        """When overridden, triggered_rules is empty (Reason shows 'Trim acknowledged')."""
        pos = _make_position(cost_basis=100.0, current_price=115.0, trim_acknowledged=True)
        result = _enrich_position(pos)
        assert result["triggered_rules"] == []

    def test_trim_not_acknowledged_shows_trim(self):
        """Without flag, a position 15% above cost basis shows Trim."""
        pos = _make_position(cost_basis=100.0, current_price=115.0, trim_acknowledged=False)
        result = _enrich_position(pos)
        assert result["verdict"] == Verdict.trim
        assert result["trim_acknowledged"] is False

    def test_trim_acknowledged_none_treated_as_false(self):
        """trim_acknowledged=None (DB default) behaves like False — Trim is not suppressed."""
        pos = _make_position(cost_basis=100.0, current_price=115.0, trim_acknowledged=None)
        result = _enrich_position(pos)
        assert result["verdict"] == Verdict.trim
        assert result["trim_acknowledged"] is False

    def test_sell_not_suppressed_by_trim_acknowledged(self):
        """A Sell rule firing must not be suppressed even when trim_acknowledged=True."""
        pos = _make_position(
            cost_basis=100.0,
            current_price=115.0,
            investment_type=InvestmentType.long_term,
            trim_acknowledged=True,
        )
        # Inject a weekly MA signal that triggers the Sell rule (close below SMA-20)
        indicator_cache = {("weekly", 20): (140.0, 150.0)}
        result = _enrich_position(pos, indicator_cache=indicator_cache)
        assert result["verdict"] == Verdict.sell
        assert result["trim_acknowledged"] is False

    def test_hold_position_flag_has_no_visible_effect(self):
        """A position at 5% gain (Hold) stays Hold regardless of trim_acknowledged."""
        pos = _make_position(cost_basis=100.0, current_price=105.0, trim_acknowledged=True)
        result = _enrich_position(pos)
        # No Trim rule fires (5% < 10%), so verdict is Hold without any override
        assert result["verdict"] == Verdict.hold
        assert result["trim_acknowledged"] is False  # override did not fire

    def test_trim_acknowledged_key_false_when_no_override(self):
        """trim_acknowledged in the result dict is False whenever the override didn't fire."""
        pos = _make_position(cost_basis=100.0, current_price=105.0, trim_acknowledged=True)
        result = _enrich_position(pos)
        assert result["trim_acknowledged"] is False


# ---------------------------------------------------------------------------
# Route tests: POST /trim-acknowledge and /trim-unacknowledge
# ---------------------------------------------------------------------------


class TestTrimAcknowledgeRoutes:
    def _add_position(self, db_factory) -> int:
        """Insert a position at 15% gain (would show Trim)."""
        db = db_factory()
        try:
            pos = Position(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2024, 1, 1),
                investment_type=InvestmentType.long_term,
                current_price=115.0,
            )
            db.add(pos)
            db.commit()
            pos_id = pos.id
        finally:
            db.close()
        return pos_id

    def test_trim_acknowledge_sets_flag(self, client, _setup_db):
        """POST /trim-acknowledge/{id} sets trim_acknowledged=True and redirects to /."""
        pos_id = self._add_position(_setup_db)
        form = csrf_form_data(client)
        resp = client.post(f"/trim-acknowledge/{pos_id}", data=form, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        db = _setup_db()
        try:
            pos = db.get(Position, pos_id)
            assert pos.trim_acknowledged is True
        finally:
            db.close()

    def test_trim_unacknowledge_clears_flag(self, client, _setup_db):
        """POST /trim-unacknowledge/{id} clears trim_acknowledged=False and redirects to /."""
        pos_id = self._add_position(_setup_db)

        db = _setup_db()
        try:
            pos = db.get(Position, pos_id)
            pos.trim_acknowledged = True
            db.commit()
        finally:
            db.close()

        form = csrf_form_data(client)
        resp = client.post(f"/trim-unacknowledge/{pos_id}", data=form, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        db = _setup_db()
        try:
            pos = db.get(Position, pos_id)
            assert pos.trim_acknowledged is False
        finally:
            db.close()

    def test_trim_acknowledge_unknown_position_still_redirects(self, client, _setup_db):
        """A POST to a non-existent position ID should still redirect gracefully."""
        form = csrf_form_data(client)
        resp = client.post("/trim-acknowledge/99999", data=form, follow_redirects=False)
        assert resp.status_code == 303

    def test_trim_unacknowledge_unknown_position_still_redirects(self, client, _setup_db):
        """A POST to a non-existent position ID should still redirect gracefully."""
        form = csrf_form_data(client)
        resp = client.post("/trim-unacknowledge/99999", data=form, follow_redirects=False)
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Integration tests: portfolio page HTML rendering
# ---------------------------------------------------------------------------


class TestPortfolioTrimUI:
    def _add_position(self, db_factory, **kwargs) -> int:
        db = db_factory()
        try:
            defaults = dict(
                ticker="AAPL",
                company_name="Apple Inc.",
                cost_basis=100.0,
                initial_purchase_date=date(2024, 1, 1),
                investment_type=InvestmentType.long_term,
                current_price=115.0,
            )
            defaults.update(kwargs)
            pos = Position(**defaults)
            db.add(pos)
            db.commit()
            pos_id = pos.id
        finally:
            db.close()
        return pos_id

    def test_mark_as_trimmed_button_shown_for_trim_verdict(self, client, _setup_db):
        """A position at 15% gain (Trim verdict) should show the 'Mark as Trimmed' button."""
        self._add_position(_setup_db, cost_basis=100.0, current_price=115.0)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Mark as Trimmed" in resp.text

    def test_clear_trim_button_shown_when_acknowledged(self, client, _setup_db):
        """After acknowledging, 'Clear Trim' replaces 'Mark as Trimmed'."""
        pos_id = self._add_position(_setup_db, cost_basis=100.0, current_price=115.0)
        db = _setup_db()
        try:
            pos = db.get(Position, pos_id)
            pos.trim_acknowledged = True
            db.commit()
        finally:
            db.close()

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Clear Trim" in resp.text
        assert "Mark as Trimmed" not in resp.text

    def test_trimmed_checkmark_shown_when_acknowledged(self, client, _setup_db):
        """'Trimmed ✓' note should appear in the Verdict cell when acknowledged."""
        pos_id = self._add_position(_setup_db, cost_basis=100.0, current_price=115.0)
        db = _setup_db()
        try:
            pos = db.get(Position, pos_id)
            pos.trim_acknowledged = True
            db.commit()
        finally:
            db.close()

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Trimmed ✓" in resp.text

    def test_trim_acknowledged_tag_shown_in_reason(self, client, _setup_db):
        """'Trim acknowledged' rule-tag should appear in the Reason cell when acknowledged."""
        pos_id = self._add_position(_setup_db, cost_basis=100.0, current_price=115.0)
        db = _setup_db()
        try:
            pos = db.get(Position, pos_id)
            pos.trim_acknowledged = True
            db.commit()
        finally:
            db.close()

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Trim acknowledged" in resp.text

    def test_no_trim_buttons_for_hold_position(self, client, _setup_db):
        """A Hold position (5% gain) should show neither 'Mark as Trimmed' nor 'Clear Trim'."""
        self._add_position(_setup_db, cost_basis=100.0, current_price=105.0)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "Mark as Trimmed" not in resp.text
        assert "Clear Trim" not in resp.text
