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
        assert "SELL_MA_ALL" in resp.text
        assert "Moving average conditions" in resp.text
        assert "Order" not in resp.text

    def test_rules_page_shows_default_ma_conditions(self, client):
        resp = client.get("/rules")
        assert resp.status_code == 200
        # Long-term defaults to weekly SMA-20
        assert "SMA-20" in resp.text
        # Short-term defaults to daily SMA-21
        assert "SMA-21" in resp.text

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

    def test_add_ma_condition(self, client):
        resp = client.post(
            "/rules/long-term/SELL_MA_ALL/conditions/add",
            data={"interval": "daily", "time_period": "10"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # Verify condition was added
        page = client.get("/rules")
        assert "SMA-10" in page.text

    def test_remove_ma_condition(self, client):
        # First add a second condition
        client.post(
            "/rules/long-term/SELL_MA_ALL/conditions/add",
            data={"interval": "daily", "time_period": "10"},
            follow_redirects=False,
        )
        # Then remove the default weekly-20
        resp = client.post(
            "/rules/long-term/SELL_MA_ALL/conditions/delete",
            data={"interval": "weekly", "time_period": "20"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        page = client.get("/rules")
        # weekly SMA-20 should be gone for long-term but daily SMA-10 should remain
        assert "SMA-10" in page.text

    def test_cannot_remove_last_condition_when_enabled(self, client):
        """Cannot remove the only remaining condition when the rule is enabled."""
        resp = client.post(
            "/rules/long-term/SELL_MA_ALL/conditions/delete",
            data={"interval": "weekly", "time_period": "20"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # The condition should still be present since it's the last one
        page = client.get("/rules")
        assert "SMA-20" in page.text

    def test_cannot_enable_sell_ma_all_with_no_conditions(self, client, _setup_db):
        """Enabling SELL_MA_ALL when conditions list is empty is rejected."""
        db = _setup_db()
        try:
            from app.rule_config import ensure_strategy_rule_defaults
            ensure_strategy_rule_defaults(db)
            # Clear conditions so the rule has an empty params_json
            row = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.investment_type == "long-term")
                .filter(StrategyRuleConfig.rule_key == "SELL_MA_ALL")
                .first()
            )
            assert row is not None
            row.enabled = False
            row.params_json = '{"conditions": []}'
            db.commit()
        finally:
            db.close()

        # Attempt to enable the rule; should redirect (route catches ValueError)
        resp = client.post("/rules/long-term/SELL_MA_ALL", follow_redirects=False)
        assert resp.status_code == 303

        # The rule must still be disabled after the rejected enable attempt
        db2 = _setup_db()
        try:
            row = (
                db2.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.investment_type == "long-term")
                .filter(StrategyRuleConfig.rule_key == "SELL_MA_ALL")
                .first()
            )
            assert row is not None
            assert row.enabled is False
        finally:
            db2.close()

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

    def test_deprecated_sell_rules_are_cleaned_up(self, _setup_db):
        """Old hardcoded sell rule rows should be removed on defaults seeding."""
        db = _setup_db()
        try:
            # Simulate old rule config rows
            db.add(StrategyRuleConfig(
                investment_type="long-term",
                rule_key="LT-SELL-20W-MA",
                enabled=True,
            ))
            db.add(StrategyRuleConfig(
                investment_type="short-term",
                rule_key="ST-SELL-21D-MA",
                enabled=True,
            ))
            db.commit()

            # Trigger defaults seeding (which should clean up deprecated keys)
            from app.rule_config import ensure_strategy_rule_defaults
            ensure_strategy_rule_defaults(db)

            old_rows = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.rule_key.in_(["LT-SELL-20W-MA", "ST-SELL-21D-MA"]))
                .all()
            )
            assert len(old_rows) == 0

            # SELL_MA_ALL should exist for both types
            ma_rows = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.rule_key == "SELL_MA_ALL")
                .all()
            )
            assert len(ma_rows) == 2
        finally:
            db.close()
