"""Unit tests for the rule engine.

Tests cover each individual rule function, the evaluate_position orchestrator,
and edge cases such as zero gain, breakeven, and boundary conditions.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from app.rule_engine import (
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
        result = rule_long_term_sell_below_20w_ma(pos, weekly_close_below_20w_ma=True)
        assert result is not None
        assert result.verdict == Verdict.sell
        assert result.rule_label == "LT-SELL-20W-MA"

    def test_does_not_trigger_when_above_ma(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        result = rule_long_term_sell_below_20w_ma(pos, weekly_close_below_20w_ma=False)
        assert result is None

    def test_does_not_trigger_for_short_term(self):
        pos = FakePosition(investment_type=InvestmentType.short_term)
        result = rule_long_term_sell_below_20w_ma(pos, weekly_close_below_20w_ma=True)
        assert result is None


# ---------------------------------------------------------------------------
# Tests for rule_short_term_sell_below_21d_ma
# ---------------------------------------------------------------------------


class TestShortTermSellBelow21dMA:
    def test_triggers_when_below_ma(self):
        pos = FakePosition(investment_type=InvestmentType.short_term)
        result = rule_short_term_sell_below_21d_ma(pos, daily_close_below_21d_ma=True)
        assert result is not None
        assert result.verdict == Verdict.sell
        assert result.rule_label == "ST-SELL-21D-MA"

    def test_does_not_trigger_when_above_ma(self):
        pos = FakePosition(investment_type=InvestmentType.short_term)
        result = rule_short_term_sell_below_21d_ma(pos, daily_close_below_21d_ma=False)
        assert result is None

    def test_does_not_trigger_for_long_term(self):
        pos = FakePosition(investment_type=InvestmentType.long_term)
        result = rule_short_term_sell_below_21d_ma(pos, daily_close_below_21d_ma=True)
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
        results = evaluate_position(pos, weekly_close_below_20w_ma=True)
        assert len(results) >= 1
        assert results[0].verdict == Verdict.sell

    def test_long_term_trim_when_no_sell(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        results = evaluate_position(pos, weekly_close_below_20w_ma=False)
        assert results[0].verdict == Verdict.trim

    def test_long_term_hold(self):
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=105.0,
        )
        results = evaluate_position(pos, weekly_close_below_20w_ma=False)
        assert results[0].verdict == Verdict.hold

    def test_short_term_sell_takes_priority(self):
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        results = evaluate_position(pos, daily_close_below_21d_ma=True)
        assert results[0].verdict == Verdict.sell

    def test_short_term_trim_when_no_sell(self):
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=115.0,
        )
        results = evaluate_position(pos, daily_close_below_21d_ma=False)
        assert results[0].verdict == Verdict.trim

    def test_short_term_hold(self):
        pos = FakePosition(
            investment_type=InvestmentType.short_term,
            cost_basis=100.0,
            current_price=105.0,
        )
        results = evaluate_position(pos, daily_close_below_21d_ma=False)
        assert results[0].verdict == Verdict.hold

    def test_no_rules_triggered_below_cost_no_ma_signal(self):
        """Position below cost basis with no MA sell signal → empty list."""
        pos = FakePosition(
            investment_type=InvestmentType.long_term,
            cost_basis=100.0,
            current_price=90.0,
        )
        results = evaluate_position(pos, weekly_close_below_20w_ma=False)
        assert len(results) == 0


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
