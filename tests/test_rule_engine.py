"""Unit tests for the rule engine.

Tests cover each individual rule function, the evaluate_position orchestrator,
and edge cases such as zero gain, breakeven, and boundary conditions.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from app.rule_engine import (
    MarketSignals,
    RULE_KEY_HOLD_ABOVE_COST,
    RULE_KEY_ST_SELL_21D_MA,
    RULE_KEY_TRIM_10PCT,
    StrategyRuleSelection,
    compute_hold_duration_days,
    compute_percent_gain,
    evaluate_position,
    get_verdict,
    rule_hold_above_cost_basis,
    rule_long_term_sell_below_20w_ma,
    rule_short_term_sell_below_21d_ma,
    rule_trim_above_10_percent,
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
# Tests for rule_long_term_sell_below_20w_ma
# ---------------------------------------------------------------------------


class TestLongTermSellBelow20wMA:
    def test_triggers_when_below_ma(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        signals = MarketSignals(weekly_close=140.0, weekly_sma_20=150.0)
        result = rule_long_term_sell_below_20w_ma(pos, signals)
        assert result is not None
        assert result.verdict == Verdict.sell
        assert result.rule_label == "LT-SELL-20W-MA"

    def test_does_not_trigger_when_above_ma(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        signals = MarketSignals(weekly_close=160.0, weekly_sma_20=150.0)
        result = rule_long_term_sell_below_20w_ma(pos, signals)
        assert result is None

    def test_does_not_trigger_when_equal_to_ma(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        signals = MarketSignals(weekly_close=150.0, weekly_sma_20=150.0)
        result = rule_long_term_sell_below_20w_ma(pos, signals)
        assert result is None

    def test_does_not_trigger_for_short_term(self):
        pos = FakePosition(investment_type=InvestmentType.short_term)
        signals = MarketSignals(weekly_close=140.0, weekly_sma_20=150.0)
        result = rule_long_term_sell_below_20w_ma(pos, signals)
        assert result is None

    def test_does_not_trigger_when_data_missing(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        signals = MarketSignals()  # no weekly data
        result = rule_long_term_sell_below_20w_ma(pos, signals)
        assert result is None


# ---------------------------------------------------------------------------
# Tests for rule_short_term_sell_below_21d_ma
# ---------------------------------------------------------------------------


class TestShortTermSellBelow21dMA:
    def test_triggers_when_below_ma(self):
        pos = FakePosition(investment_type=InvestmentType.short_term)
        signals = MarketSignals(daily_close=95.0, daily_sma_21=100.0)
        result = rule_short_term_sell_below_21d_ma(pos, signals)
        assert result is not None
        assert result.verdict == Verdict.sell
        assert result.rule_label == "ST-SELL-21D-MA"

    def test_does_not_trigger_when_above_ma(self):
        pos = FakePosition(investment_type=InvestmentType.short_term)
        signals = MarketSignals(daily_close=105.0, daily_sma_21=100.0)
        result = rule_short_term_sell_below_21d_ma(pos, signals)
        assert result is None

    def test_does_not_trigger_when_equal_to_ma(self):
        pos = FakePosition(investment_type=InvestmentType.short_term)
        signals = MarketSignals(daily_close=100.0, daily_sma_21=100.0)
        result = rule_short_term_sell_below_21d_ma(pos, signals)
        assert result is None

    def test_does_not_trigger_for_long_term(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        signals = MarketSignals(daily_close=95.0, daily_sma_21=100.0)
        result = rule_short_term_sell_below_21d_ma(pos, signals)
        assert result is None

    def test_does_not_trigger_when_data_missing(self):
        pos = FakePosition(investment_type=InvestmentType.short_term)
        signals = MarketSignals()  # no daily data
        result = rule_short_term_sell_below_21d_ma(pos, signals)
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
# Tests for evaluate_position (integration of rules)
# ---------------------------------------------------------------------------


class TestEvaluatePosition:
    def test_long_term_sell_takes_priority(self):
        """Sell rule should be first in the list when triggered."""
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,  # also triggers trim
        )
        signals = MarketSignals(weekly_close=140.0, weekly_sma_20=150.0)
        results = evaluate_position(pos, signals=signals)
        assert len(results) >= 1
        assert results[0].verdict == Verdict.sell

    def test_long_term_trim_when_no_sell(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals(weekly_close=160.0, weekly_sma_20=150.0)
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.trim

    def test_long_term_hold(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=105.0,
        )
        signals = MarketSignals(weekly_close=160.0, weekly_sma_20=150.0)
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.hold

    def test_short_term_sell_takes_priority(self):
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals(daily_close=95.0, daily_sma_21=100.0)
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.sell

    def test_short_term_trim_when_no_sell(self):
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        signals = MarketSignals(daily_close=105.0, daily_sma_21=100.0)
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.trim

    def test_short_term_hold(self):
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=105.0,
        )
        signals = MarketSignals(daily_close=105.0, daily_sma_21=100.0)
        results = evaluate_position(pos, signals=signals)
        assert results[0].verdict == Verdict.hold

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
        signals = MarketSignals(weekly_close=160.0, weekly_sma_20=150.0)
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
        signals = MarketSignals(weekly_close=160.0, weekly_sma_20=150.0)
        configured = [
            StrategyRuleSelection(rule_key=RULE_KEY_HOLD_ABOVE_COST),
            StrategyRuleSelection(rule_key=RULE_KEY_TRIM_10PCT),
        ]

        results = evaluate_position(pos, signals=signals, configured_rules=configured)

        assert results[0].rule_label == RULE_KEY_TRIM_10PCT
        assert results[0].verdict == Verdict.trim

    def test_unsupported_rule_key_for_type_is_skipped(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=105.0,
        )
        signals = MarketSignals(weekly_close=160.0, weekly_sma_20=150.0)
        configured = [
            StrategyRuleSelection(rule_key=RULE_KEY_ST_SELL_21D_MA),
            StrategyRuleSelection(rule_key=RULE_KEY_HOLD_ABOVE_COST),
        ]

        results = evaluate_position(pos, signals=signals, configured_rules=configured)

        assert len(results) == 1
        assert results[0].rule_label == RULE_KEY_HOLD_ABOVE_COST


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
