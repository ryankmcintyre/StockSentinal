"""Unit tests for the rule engine.

Tests cover individual rule functions, the SELL_MA_ALL composite rule,
the evaluate_position orchestrator, and edge cases.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from app.rule_engine import (
    MarketSignals,
    MAX_MA_CONDITIONS,
    RULE_KEY_HOLD_ABOVE_COST,
    RULE_KEY_SELL_MA_ALL,
    RULE_KEY_TRIM_10PCT,
    StrategyRuleSelection,
    compute_hold_duration_days,
    compute_percent_gain,
    evaluate_position,
    get_verdict,
    parse_params_json,
    rule_hold_above_cost_basis,
    rule_sell_ma_all,
    rule_trim_above_10_percent,
    validate_ma_conditions,
)
from app.schemas import InvestmentType, Verdict


# ---------------------------------------------------------------------------
# Test fixture: lightweight position dataclass
# ---------------------------------------------------------------------------


@dataclass
class FakePosition:
    ticker: str = "AAPL"
    cost_basis: float = 100.0
    current_price: float = 110.0
    investment_type: str = InvestmentType.long_term
    initial_purchase_date: date = date(2024, 1, 1)


# ---------------------------------------------------------------------------
# Tests for rule_sell_ma_all (composite ALL-semantics)
# ---------------------------------------------------------------------------


class TestSellMaAll:
    def _signals_with(self, entries: dict[tuple[str, int], tuple[float, float]]) -> MarketSignals:
        return MarketSignals(ma_signals=entries)

    def _params(self, conditions: list[dict]) -> dict:
        return {"conditions": conditions}

    def test_single_condition_met(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        signals = self._signals_with({("weekly", 20): (140.0, 150.0)})
        params = self._params([{"interval": "weekly", "time_period": 20}])
        result = rule_sell_ma_all(pos, signals, params)
        assert result is not None
        assert result.verdict == Verdict.sell
        assert "Weekly" in result.description

    def test_single_condition_not_met(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        signals = self._signals_with({("weekly", 20): (160.0, 150.0)})
        params = self._params([{"interval": "weekly", "time_period": 20}])
        result = rule_sell_ma_all(pos, signals, params)
        assert result is None

    def test_all_conditions_met(self):
        """Sell triggers when ALL conditions are met."""
        pos = FakePosition(investment_type=InvestmentType.short_term)
        signals = self._signals_with({
            ("daily", 10): (95.0, 100.0),
            ("weekly", 8): (42.0, 45.0),
        })
        params = self._params([
            {"interval": "daily", "time_period": 10},
            {"interval": "weekly", "time_period": 8},
        ])
        result = rule_sell_ma_all(pos, signals, params)
        assert result is not None
        assert result.verdict == Verdict.sell

    def test_one_condition_not_met_blocks_sell(self):
        """If one condition fails, Sell does not trigger."""
        pos = FakePosition(investment_type=InvestmentType.short_term)
        signals = self._signals_with({
            ("daily", 10): (95.0, 100.0),  # met
            ("weekly", 8): (50.0, 45.0),   # not met (close > sma)
        })
        params = self._params([
            {"interval": "daily", "time_period": 10},
            {"interval": "weekly", "time_period": 8},
        ])
        result = rule_sell_ma_all(pos, signals, params)
        assert result is None

    def test_missing_data_prevents_sell(self):
        """If data is missing for any condition, Sell does not trigger."""
        pos = FakePosition(investment_type=InvestmentType.long_term)
        signals = self._signals_with({("weekly", 20): (140.0, 150.0)})
        params = self._params([
            {"interval": "weekly", "time_period": 20},
            {"interval": "daily", "time_period": 10},  # no data
        ])
        result = rule_sell_ma_all(pos, signals, params)
        assert result is None

    def test_none_params_returns_none(self):
        pos = FakePosition()
        signals = MarketSignals()
        result = rule_sell_ma_all(pos, signals, None)
        assert result is None

    def test_empty_conditions_returns_none(self):
        pos = FakePosition()
        signals = MarketSignals()
        result = rule_sell_ma_all(pos, signals, {"conditions": []})
        assert result is None

    def test_close_equal_to_sma_does_not_trigger(self):
        pos = FakePosition()
        signals = self._signals_with({("weekly", 20): (150.0, 150.0)})
        params = self._params([{"interval": "weekly", "time_period": 20}])
        result = rule_sell_ma_all(pos, signals, params)
        assert result is None

    def test_null_sma_value_prevents_sell(self):
        pos = FakePosition()
        signals = self._signals_with({("weekly", 20): (140.0, None)})
        params = self._params([{"interval": "weekly", "time_period": 20}])
        result = rule_sell_ma_all(pos, signals, params)
        assert result is None

    def test_null_close_value_prevents_sell(self):
        pos = FakePosition()
        signals = self._signals_with({("weekly", 20): (None, 150.0)})
        params = self._params([{"interval": "weekly", "time_period": 20}])
        result = rule_sell_ma_all(pos, signals, params)
        assert result is None


# ---------------------------------------------------------------------------
# Tests for rule_trim_above_10_percent
# ---------------------------------------------------------------------------


class TestTrimAbove10Percent:
    def test_triggers_when_above_10_percent(self):
        pos = FakePosition(cost_basis=100.0, current_price=111.0)
        result = rule_trim_above_10_percent(pos)
        assert result is not None
        assert result.verdict == Verdict.trim
        assert result.rule_label == "TRIM-10PCT"

    def test_does_not_trigger_at_exactly_10_percent(self):
        pos = FakePosition(cost_basis=100.0, current_price=110.0)
        result = rule_trim_above_10_percent(pos)
        assert result is None

    def test_does_not_trigger_below_10_percent(self):
        pos = FakePosition(cost_basis=100.0, current_price=105.0)
        result = rule_trim_above_10_percent(pos)
        assert result is None

    def test_does_not_trigger_at_loss(self):
        pos = FakePosition(cost_basis=100.0, current_price=90.0)
        result = rule_trim_above_10_percent(pos)
        assert result is None

    def test_does_not_trigger_zero_cost_basis(self):
        pos = FakePosition(cost_basis=0.0, current_price=50.0)
        result = rule_trim_above_10_percent(pos)
        assert result is None

    def test_applies_to_short_term(self):
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        result = rule_trim_above_10_percent(pos)
        assert result is not None
        assert result.verdict == Verdict.trim


# ---------------------------------------------------------------------------
# Tests for rule_hold_above_cost_basis
# ---------------------------------------------------------------------------


class TestHoldAboveCostBasis:
    def test_triggers_when_above_cost_basis(self):
        pos = FakePosition(cost_basis=100.0, current_price=105.0)
        result = rule_hold_above_cost_basis(pos)
        assert result is not None
        assert result.verdict == Verdict.hold

    def test_triggers_at_breakeven(self):
        pos = FakePosition(cost_basis=100.0, current_price=100.0)
        result = rule_hold_above_cost_basis(pos)
        assert result is not None
        assert result.verdict == Verdict.hold

    def test_does_not_trigger_below_cost_basis(self):
        pos = FakePosition(cost_basis=100.0, current_price=99.99)
        result = rule_hold_above_cost_basis(pos)
        assert result is None


# ---------------------------------------------------------------------------
# Tests for compute_percent_gain
# ---------------------------------------------------------------------------


class TestComputePercentGain:
    def test_positive_gain(self):
        assert compute_percent_gain(100.0, 115.0) == pytest.approx(15.0)

    def test_zero_gain(self):
        assert compute_percent_gain(100.0, 100.0) == pytest.approx(0.0)

    def test_negative_gain(self):
        assert compute_percent_gain(100.0, 80.0) == pytest.approx(-20.0)

    def test_zero_cost_basis_returns_zero(self):
        assert compute_percent_gain(0.0, 50.0) == 0.0


# ---------------------------------------------------------------------------
# Tests for compute_hold_duration_days
# ---------------------------------------------------------------------------


class TestComputeHoldDurationDays:
    def test_purchased_today(self):
        assert compute_hold_duration_days(date.today()) == 0

    def test_purchased_30_days_ago(self):
        purchase = date.today() - timedelta(days=30)
        assert compute_hold_duration_days(purchase) == 30


# ---------------------------------------------------------------------------
# Tests for validate_ma_conditions
# ---------------------------------------------------------------------------


class TestValidateMaConditions:
    def test_valid_conditions(self):
        errors = validate_ma_conditions([
            {"interval": "daily", "time_period": 21},
            {"interval": "weekly", "time_period": 20},
        ])
        assert errors == []

    def test_invalid_interval(self):
        errors = validate_ma_conditions([{"interval": "monthly", "time_period": 10}])
        assert len(errors) == 1
        assert "interval" in errors[0]

    def test_period_too_low(self):
        errors = validate_ma_conditions([{"interval": "daily", "time_period": 1}])
        assert len(errors) == 1
        assert "time_period" in errors[0]

    def test_period_too_high(self):
        errors = validate_ma_conditions([{"interval": "daily", "time_period": 201}])
        assert len(errors) == 1

    def test_duplicate_condition(self):
        errors = validate_ma_conditions([
            {"interval": "daily", "time_period": 21},
            {"interval": "daily", "time_period": 21},
        ])
        assert len(errors) == 1
        assert "duplicate" in errors[0].lower()

    def test_too_many_conditions(self):
        conditions = [{"interval": "daily", "time_period": i} for i in range(2, 2 + MAX_MA_CONDITIONS + 1)]
        errors = validate_ma_conditions(conditions)
        assert any("Maximum" in e for e in errors)

    def test_non_integer_period(self):
        errors = validate_ma_conditions([{"interval": "daily", "time_period": "abc"}])
        assert len(errors) == 1


# ---------------------------------------------------------------------------
# Tests for parse_params_json
# ---------------------------------------------------------------------------


class TestParseParamsJson:
    def test_valid_json(self):
        result = parse_params_json('{"conditions": []}')
        assert result == {"conditions": []}

    def test_none_returns_none(self):
        assert parse_params_json(None) is None

    def test_invalid_json_returns_none(self):
        assert parse_params_json("not json") is None


# ---------------------------------------------------------------------------
# Tests for evaluate_position (integration of rules)
# ---------------------------------------------------------------------------


class TestEvaluatePosition:
    def test_sell_triggers_with_ma_conditions(self):
        """Sell rule should fire when configured MA conditions are met."""
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals(
            ma_signals={("weekly", 20): (140.0, 150.0)},
        )
        configured = [
            StrategyRuleSelection(
                rule_key=RULE_KEY_SELL_MA_ALL,
                params={"conditions": [{"interval": "weekly", "time_period": 20}]},
            ),
            StrategyRuleSelection(rule_key=RULE_KEY_TRIM_10PCT),
            StrategyRuleSelection(rule_key=RULE_KEY_HOLD_ABOVE_COST),
        ]
        results = evaluate_position(pos, signals=signals, configured_rules=configured)
        assert len(results) >= 1
        assert results[0].verdict == Verdict.sell

    def test_trim_when_sell_conditions_not_met(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals(
            ma_signals={("weekly", 20): (160.0, 150.0)},  # close > sma
        )
        configured = [
            StrategyRuleSelection(
                rule_key=RULE_KEY_SELL_MA_ALL,
                params={"conditions": [{"interval": "weekly", "time_period": 20}]},
            ),
            StrategyRuleSelection(rule_key=RULE_KEY_TRIM_10PCT),
            StrategyRuleSelection(rule_key=RULE_KEY_HOLD_ABOVE_COST),
        ]
        results = evaluate_position(pos, signals=signals, configured_rules=configured)
        assert results[0].verdict == Verdict.trim

    def test_hold_when_no_sell_no_trim(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=105.0,
        )
        signals = MarketSignals(
            ma_signals={("weekly", 20): (160.0, 150.0)},
        )
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.hold

    def test_short_term_sell_with_daily_condition(self):
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals(
            ma_signals={("daily", 21): (95.0, 100.0)},
        )
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.sell

    def test_no_rules_triggered_below_cost_no_market_data(self):
        """Position below cost basis with no market data → empty list."""
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=90.0,
        )
        results = evaluate_position(pos)
        assert len(results) == 0

    def test_no_signals_suppresses_sell_rule(self):
        """Without market signals the MA sell rules cannot fire."""
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        results = evaluate_position(pos)  # no signals
        assert all(r.verdict != Verdict.sell for r in results)

    def test_configured_rules_only_apply_selected_rules(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals()
        configured = [StrategyRuleSelection(rule_key=RULE_KEY_HOLD_ABOVE_COST)]

        results = evaluate_position(pos, signals=signals, configured_rules=configured)

        assert len(results) == 1
        assert results[0].rule_label == RULE_KEY_HOLD_ABOVE_COST
        assert results[0].verdict == Verdict.hold

    def test_verdict_priority_overrides_input_selection_order(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals()
        configured = [
            StrategyRuleSelection(rule_key=RULE_KEY_HOLD_ABOVE_COST),
            StrategyRuleSelection(rule_key=RULE_KEY_TRIM_10PCT),
        ]

        results = evaluate_position(pos, signals=signals, configured_rules=configured)

        assert results[0].rule_label == RULE_KEY_TRIM_10PCT
        assert results[0].verdict == Verdict.trim

    def test_default_long_term_preserves_weekly_20_behavior(self):
        """Default selections for long-term should use weekly MA-20."""
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals(
            ma_signals={("weekly", 20): (140.0, 150.0)},
        )
        # Use defaults (no configured_rules)
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.sell

    def test_default_short_term_preserves_daily_21_behavior(self):
        """Default selections for short-term should use daily MA-21."""
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals(
            ma_signals={("daily", 21): (95.0, 100.0)},
        )
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.sell


# ---------------------------------------------------------------------------
# Tests for get_verdict
# ---------------------------------------------------------------------------


class TestGetVerdict:
    def test_returns_first_verdict(self):
        from app.schemas import RuleResult

        rules = [
            RuleResult(rule_label="A", verdict=Verdict.sell, description="test"),
            RuleResult(rule_label="B", verdict=Verdict.hold, description="test"),
        ]
        assert get_verdict(rules) == Verdict.sell

    def test_empty_list_defaults_to_hold(self):
        assert get_verdict([]) == Verdict.hold
