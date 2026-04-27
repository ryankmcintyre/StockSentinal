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


class TestExtensionAtrRules:
    """Issue #18: ATR extension rules in catalog + UI + indicator requirements."""

    def test_rules_page_lists_extension_rules(self, client):
        resp = client.get("/rules")
        assert resp.status_code == 200
        assert "TRIM_EXTENSION_ATR" in resp.text
        assert "SELL_EXTENSION_ATR" in resp.text
        # Default thresholds and formula bits should render
        assert "ATR-14" in resp.text
        assert "SMA-50" in resp.text

    def test_extension_rules_default_to_disabled(self, _setup_db):
        db = _setup_db()
        try:
            from app.rule_config import ensure_strategy_rule_defaults
            ensure_strategy_rule_defaults(db)
            rows = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.rule_key.in_(
                    ["TRIM_EXTENSION_ATR", "SELL_EXTENSION_ATR"]
                ))
                .all()
            )
            assert len(rows) == 4  # 2 rules × 2 strategies
            assert all(row.enabled is False for row in rows)
            # Each row should be seeded with default params_json
            assert all(row.params_json is not None for row in rows)
        finally:
            db.close()

    def test_user_can_enable_extension_rule(self, client, _setup_db):
        resp = client.post(
            "/rules/long-term/TRIM_EXTENSION_ATR",
            data={"enabled": "1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = _setup_db()
        try:
            row = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.investment_type == "long-term")
                .filter(StrategyRuleConfig.rule_key == "TRIM_EXTENSION_ATR")
                .first()
            )
            assert row is not None
            assert row.enabled is True
        finally:
            db.close()

    def test_required_indicators_includes_extension_sma_when_enabled(self, _setup_db):
        from app.rule_config import (
            ensure_strategy_rule_defaults,
            get_required_atr_indicators,
            get_required_indicators,
            update_strategy_rule_config,
        )

        db = _setup_db()
        try:
            ensure_strategy_rule_defaults(db)
            # Disable SELL_MA_ALL to isolate extension-rule indicator demands.
            update_strategy_rule_config(db, "long-term", "SELL_MA_ALL", enabled=False)
            update_strategy_rule_config(db, "short-term", "SELL_MA_ALL", enabled=False)

            assert get_required_indicators(db) == set()
            assert get_required_atr_indicators(db) == set()

            update_strategy_rule_config(db, "long-term", "SELL_EXTENSION_ATR", enabled=True)

            assert get_required_indicators(db) == {("daily", 50)}
            assert get_required_atr_indicators(db) == {("daily", 14)}
        finally:
            db.close()

    def test_required_atr_indicators_empty_when_only_trim_disabled(self, _setup_db):
        from app.rule_config import (
            ensure_strategy_rule_defaults,
            get_required_atr_indicators,
            update_strategy_rule_config,
        )

        db = _setup_db()
        try:
            ensure_strategy_rule_defaults(db)
            update_strategy_rule_config(db, "long-term", "SELL_MA_ALL", enabled=False)
            update_strategy_rule_config(db, "short-term", "SELL_MA_ALL", enabled=False)
            assert get_required_atr_indicators(db) == set()
        finally:
            db.close()

    def test_extension_sell_triggers_in_portfolio_view(self, client, _setup_db):
        """End-to-end: enable SELL_EXTENSION_ATR, seed cache, expect Sell verdict."""
        from datetime import date as _date, datetime as _dt
        from app.models import MarketAtrCache, MarketIndicatorCache
        from app.rule_config import update_strategy_rule_config

        db = _setup_db()
        try:
            # Position with current_price = 200, cost = 50.
            db.add(
                Position(
                    ticker="EXT",
                    company_name="Extended Co.",
                    cost_basis=50.0,
                    initial_purchase_date=_date(2025, 1, 1),
                    investment_type="long-term",
                    current_price=200.0,
                    notes=None,
                )
            )
            # Seed SMA-50 daily = 100, ATR-14 daily = 5.
            # ratio = (200 - 100) / 5 = 20 → triggers SELL (>=10).
            db.add(
                MarketIndicatorCache(
                    ticker="EXT",
                    interval="daily",
                    time_period=50,
                    sma_value=100.0,
                    sma_date=_date(2025, 6, 1),
                    close_value=200.0,
                    close_date=_date(2025, 6, 1),
                    retrieved_at=_dt.now(),
                )
            )
            db.add(
                MarketAtrCache(
                    ticker="EXT",
                    interval="daily",
                    time_period=14,
                    atr_value=5.0,
                    atr_date=_date(2025, 6, 1),
                    retrieved_at=_dt.now(),
                )
            )
            db.commit()
            # Enable the extension sell rule; disable SELL_MA_ALL to avoid
            # interfering signals on this synthetic position.
            update_strategy_rule_config(db, "long-term", "SELL_MA_ALL", enabled=False)
            update_strategy_rule_config(db, "long-term", "SELL_EXTENSION_ATR", enabled=True)
        finally:
            db.close()

        resp = client.get("/")
        assert resp.status_code == 200
        # The verdict should be SELL and the description text should appear.
        assert "Sell" in resp.text
        assert "Price extended" in resp.text


class TestWeeklyUpperWickRule:
    """Issue #19: weekly upper-wick reversal rule in catalog + UI + lookback."""

    def test_rules_page_lists_upper_wick_rule(self, client):
        resp = client.get("/rules")
        assert resp.status_code == 200
        assert "TRIM_WEEKLY_UPPER_WICK" in resp.text
        # Default thresholds and lookback should render
        assert "0.6" in resp.text  # upper_wick_ratio_min
        assert "26" in resp.text   # lookback_high_weeks

    def test_upper_wick_rule_default_disabled_with_seeded_params(self, _setup_db):
        from app.rule_config import ensure_strategy_rule_defaults

        db = _setup_db()
        try:
            ensure_strategy_rule_defaults(db)
            rows = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.rule_key == "TRIM_WEEKLY_UPPER_WICK")
                .all()
            )
            assert len(rows) == 2  # 1 rule × 2 strategies
            assert all(row.enabled is False for row in rows)
            assert all(row.params_json is not None for row in rows)
        finally:
            db.close()

    def test_user_can_enable_upper_wick_rule(self, client, _setup_db):
        resp = client.post(
            "/rules/long-term/TRIM_WEEKLY_UPPER_WICK",
            data={"enabled": "1"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db = _setup_db()
        try:
            row = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.investment_type == "long-term")
                .filter(StrategyRuleConfig.rule_key == "TRIM_WEEKLY_UPPER_WICK")
                .first()
            )
            assert row is not None
            assert row.enabled is True
        finally:
            db.close()

    def test_required_weekly_bar_lookback_returns_zero_when_disabled(self, _setup_db):
        from app.rule_config import (
            ensure_strategy_rule_defaults,
            get_required_weekly_bar_lookback,
        )

        db = _setup_db()
        try:
            ensure_strategy_rule_defaults(db)
            assert get_required_weekly_bar_lookback(db) == 0
        finally:
            db.close()

    def test_required_weekly_bar_lookback_returns_max_across_strategies(self, _setup_db):
        from app.rule_config import (
            ensure_strategy_rule_defaults,
            get_required_weekly_bar_lookback,
            update_strategy_rule_config,
        )

        db = _setup_db()
        try:
            ensure_strategy_rule_defaults(db)
            update_strategy_rule_config(
                db, "long-term", "TRIM_WEEKLY_UPPER_WICK", enabled=True
            )
            assert get_required_weekly_bar_lookback(db) == 26
        finally:
            db.close()

    def test_upper_wick_triggers_in_portfolio_view(self, client, _setup_db):
        """End-to-end: enable rule, seed weekly bar cache, expect Trim verdict."""
        from datetime import date as _date, datetime as _dt
        from app.models import MarketWeeklyBarCache
        from app.rule_config import update_strategy_rule_config

        db = _setup_db()
        try:
            db.add(
                Position(
                    ticker="WIK",
                    company_name="Wick Co.",
                    cost_basis=80.0,
                    initial_purchase_date=_date(2025, 1, 1),
                    investment_type="long-term",
                    current_price=95.0,
                    notes=None,
                )
            )
            # Latest weekly bar shows long upper wick near recent high.
            # range=8, body=1 (12.5%), upper_wick=5 (62.5%), close 5% below high
            db.add(
                MarketWeeklyBarCache(
                    ticker="WIK",
                    bar_date=_date(2025, 6, 6),
                    open=94.0,
                    high=100.0,
                    low=92.0,
                    close=95.0,
                    volume=1000000.0,
                    retrieved_at=_dt.now(),
                )
            )
            db.commit()
            # Disable SELL_MA_ALL to avoid interference; enable upper-wick rule.
            update_strategy_rule_config(db, "long-term", "SELL_MA_ALL", enabled=False)
            update_strategy_rule_config(
                db, "long-term", "TRIM_WEEKLY_UPPER_WICK", enabled=True
            )
        finally:
            db.close()

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Trim" in resp.text
        assert "upper-wick" in resp.text.lower()


class TestDistributionClusterRules:
    """Issue #20: clustered high-volume red-week rules."""

    def test_rules_page_lists_distribution_rules(self, client):
        resp = client.get("/rules")
        assert resp.status_code == 200
        assert "TRIM_WEEKLY_DISTRIBUTION_CLUSTER" in resp.text
        assert "SELL_WEEKLY_DISTRIBUTION_CLUSTER" in resp.text
        # Default volume multiplier and cluster window should render
        assert "1.5" in resp.text
        assert "20" in resp.text

    def test_distribution_rules_default_disabled_with_seeded_params(self, _setup_db):
        from app.rule_config import ensure_strategy_rule_defaults

        db = _setup_db()
        try:
            ensure_strategy_rule_defaults(db)
            rows = (
                db.query(StrategyRuleConfig)
                .filter(StrategyRuleConfig.rule_key.in_(
                    ["TRIM_WEEKLY_DISTRIBUTION_CLUSTER", "SELL_WEEKLY_DISTRIBUTION_CLUSTER"]
                ))
                .all()
            )
            assert len(rows) == 4  # 2 rules × 2 strategies
            assert all(row.enabled is False for row in rows)
            assert all(row.params_json is not None for row in rows)
        finally:
            db.close()

    def test_required_weekly_bar_lookback_uses_distribution_baseline(self, _setup_db):
        from app.rule_config import (
            ensure_strategy_rule_defaults,
            get_required_weekly_bar_lookback,
            update_strategy_rule_config,
        )

        db = _setup_db()
        try:
            ensure_strategy_rule_defaults(db)
            update_strategy_rule_config(
                db, "long-term", "SELL_WEEKLY_DISTRIBUTION_CLUSTER", enabled=True
            )
            # max(baseline=20, cluster_window=8) = 20
            assert get_required_weekly_bar_lookback(db) == 20
        finally:
            db.close()

    def test_distribution_sell_triggers_in_portfolio_view(self, client, _setup_db):
        """End-to-end: enable SELL distribution rule, seed weekly bars, expect Sell."""
        from datetime import date as _date, datetime as _dt, timedelta as _td
        from app.models import MarketWeeklyBarCache
        from app.rule_config import update_strategy_rule_config

        db = _setup_db()
        try:
            db.add(
                Position(
                    ticker="DST",
                    company_name="Dist Co.",
                    cost_basis=80.0,
                    initial_purchase_date=_date(2025, 1, 1),
                    investment_type="long-term",
                    current_price=85.0,
                    notes=None,
                )
            )
            # 3 high-vol red weeks recent + 12 normal red weeks for baseline.
            d = _date(2025, 6, 13)
            for _ in range(3):
                db.add(MarketWeeklyBarCache(
                    ticker="DST", bar_date=d, open=100, high=100, low=85, close=90,
                    volume=300.0, retrieved_at=_dt.now(),
                ))
                d -= _td(weeks=1)
            for _ in range(20):
                db.add(MarketWeeklyBarCache(
                    ticker="DST", bar_date=d, open=100, high=100, low=85, close=90,
                    volume=100.0, retrieved_at=_dt.now(),
                ))
                d -= _td(weeks=1)
            db.commit()
            update_strategy_rule_config(db, "long-term", "SELL_MA_ALL", enabled=False)
            update_strategy_rule_config(
                db, "long-term", "SELL_WEEKLY_DISTRIBUTION_CLUSTER", enabled=True
            )
        finally:
            db.close()

        resp = client.get("/")
        assert resp.status_code == 200
        assert "Sell" in resp.text
        assert "high-volume red week" in resp.text.lower()
