"""Tests for strategy rule configuration routes and integration."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models import Base, Position, StrategyRuleConfig


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

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestingSession
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client():
    return TestClient(app)


class TestRulesPage:
    def test_rules_page_renders_default_sections(self, client):
        resp = client.get("/rules")
        assert resp.status_code == 200
        assert "Long-term strategy rules" in resp.text
        assert "Short-term strategy rules" in resp.text
        assert "LT-SELL-20W-MA" in resp.text
        assert "ST-SELL-21D-MA" in resp.text
        assert "Order" not in resp.text

    def test_post_updates_rule_configuration(self, client, _setup_db):
        resp = client.post(
            "/rules/long-term/TRIM-10PCT",
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = _setup_db()
        try:
            row = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.investment_type == "long-term")
                .filter(StrategyRuleConfig.rule_key == "TRIM-10PCT")
                .first()
            )
            assert row is not None
            assert row.enabled is False
        finally:
            db.close()

    def test_portfolio_uses_configured_rules(self, client, _setup_db):
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
                    notes=None,
                )
            )
            db.commit()
        finally:
            db.close()

        before = client.get("/")
        assert before.status_code == 200
        assert "Price is 15.0% above cost basis (&gt;10%)" in before.text

        disable_trim = client.post(
            "/rules/long-term/TRIM-10PCT",
            follow_redirects=False,
        )
        assert disable_trim.status_code == 303

        after = client.get("/")
        assert after.status_code == 200
        assert "Price is 15.0% above cost basis (&gt;10%)" not in after.text
        assert "Price is at or above cost basis" in after.text
