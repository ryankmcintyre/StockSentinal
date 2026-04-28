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
    RULE_KEY_SELL_EXTENSION_ATR,
    RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM,
    RULE_KEY_SELL_MA_ALL,
    RULE_KEY_TRIM_10PCT,
    RULE_KEY_TRIM_EXTENSION_ATR,
    RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
    RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
    RULE_KEY_TRIM_FIRST_LOWER_HIGH,
    RULE_KEY_SELL_LOWER_HIGH_LOWER_LOW,
    RULE_KEY_TRIM_WEEKLY_UPPER_WICK,
    StrategyRuleSelection,
    WeeklyOhlcBar,
    compute_hold_duration_days,
    compute_percent_gain,
    default_distribution_cluster_params,
    default_extension_atr_params,
    default_failed_breakout_params,
    default_lh_ll_params,
    default_upper_wick_params,
    evaluate_position,
    get_distribution_cluster_lookback_weeks,
    get_extension_indicator_requirements,
    get_failed_breakout_lookback_weeks,
    get_lh_ll_lookback_weeks,
    get_upper_wick_lookback_weeks,
    get_verdict,
    parse_params_json,
    rule_hold_above_cost_basis,
    rule_sell_distribution_cluster,
    rule_sell_extension_atr,
    rule_sell_failed_breakout_reclaim,
    rule_sell_lower_high_lower_low,
    rule_sell_ma_all,
    rule_trim_above_10_percent,
    rule_trim_distribution_cluster,
    rule_trim_extension_atr,
    rule_trim_first_lower_high,
    rule_trim_weekly_upper_wick,
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
    sector_benchmark_ticker: str | None = None


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


# ---------------------------------------------------------------------------
# Tests for ATR-extension rules (issue #18)
# ---------------------------------------------------------------------------


def _atr_signals(
    *,
    sma_50: float | None = 100.0,
    atr_14: float | None = 1.0,
    interval: str = "daily",
    sma_period: int = 50,
    atr_period: int = 14,
) -> MarketSignals:
    """Build a MarketSignals populated with SMA + ATR for the extension rules."""
    ma = {}
    if sma_50 is not None:
        ma[(interval, sma_period)] = (None, sma_50)
    atr = {}
    if atr_14 is not None:
        atr[(interval, atr_period)] = atr_14
    return MarketSignals(ma_signals=ma, atr_signals=atr)


class TestRuleTrimExtensionAtr:
    def test_below_threshold_does_not_trigger(self):
        # ratio = (107.99 - 100) / 1.0 = 7.99 < 8 → no trigger
        pos = FakePosition(current_price=107.99)
        assert rule_trim_extension_atr(pos, _atr_signals()) is None

    def test_at_threshold_triggers_trim(self):
        # ratio = (108 - 100) / 1.0 = 8.00 → trim
        pos = FakePosition(current_price=108.0)
        result = rule_trim_extension_atr(pos, _atr_signals())
        assert result is not None
        assert result.verdict == Verdict.trim
        assert result.rule_label == RULE_KEY_TRIM_EXTENSION_ATR

    def test_just_below_sell_threshold_still_triggers_trim(self):
        # ratio = 9.99 → trim still fires (sell rule is separate)
        pos = FakePosition(current_price=109.99)
        result = rule_trim_extension_atr(pos, _atr_signals())
        assert result is not None
        assert result.verdict == Verdict.trim

    def test_at_or_above_sell_threshold_trim_still_fires(self):
        # Trim rule is independent of sell threshold; engine precedence
        # decides the final verdict when both rules are configured.
        pos = FakePosition(current_price=110.0)
        result = rule_trim_extension_atr(pos, _atr_signals())
        assert result is not None
        assert result.verdict == Verdict.trim

    def test_missing_sma_does_not_trigger(self):
        pos = FakePosition(current_price=110.0)
        assert rule_trim_extension_atr(pos, _atr_signals(sma_50=None)) is None

    def test_missing_atr_does_not_trigger(self):
        pos = FakePosition(current_price=110.0)
        assert rule_trim_extension_atr(pos, _atr_signals(atr_14=None)) is None

    def test_zero_atr_does_not_trigger(self):
        pos = FakePosition(current_price=110.0)
        assert rule_trim_extension_atr(pos, _atr_signals(atr_14=0.0)) is None

    def test_negative_atr_does_not_trigger(self):
        pos = FakePosition(current_price=110.0)
        assert rule_trim_extension_atr(pos, _atr_signals(atr_14=-1.0)) is None

    def test_zero_or_negative_price_does_not_trigger(self):
        pos = FakePosition(current_price=0.0)
        assert rule_trim_extension_atr(pos, _atr_signals()) is None

    def test_zero_or_negative_sma_does_not_trigger(self):
        pos = FakePosition(current_price=110.0)
        assert rule_trim_extension_atr(pos, _atr_signals(sma_50=0.0)) is None

    def test_custom_threshold_via_params(self):
        # ratio = 5; threshold lowered to 4 → fires
        pos = FakePosition(current_price=105.0)
        params = default_extension_atr_params() | {"trim_threshold": 4.0}
        result = rule_trim_extension_atr(pos, _atr_signals(), params)
        assert result is not None
        assert result.verdict == Verdict.trim

    def test_custom_periods_via_params(self):
        # Configure a weekly SMA-200 / ATR-21 setup
        pos = FakePosition(current_price=120.0)
        signals = _atr_signals(
            sma_50=100.0, atr_14=2.0, interval="weekly", sma_period=200, atr_period=21,
        )
        params = {"interval": "weekly", "sma_period": 200, "atr_period": 21}
        result = rule_trim_extension_atr(pos, signals, params)
        assert result is not None
        assert result.verdict == Verdict.trim
        # ratio = (120 - 100) / 2 = 10x > default trim threshold (8)
        assert "10.00x" in result.description


class TestRuleSellExtensionAtr:
    def test_below_threshold_does_not_trigger(self):
        # ratio = 9.99 < 10 → no sell
        pos = FakePosition(current_price=109.99)
        assert rule_sell_extension_atr(pos, _atr_signals()) is None

    def test_at_threshold_triggers_sell(self):
        # ratio = 10.0 → sell
        pos = FakePosition(current_price=110.0)
        result = rule_sell_extension_atr(pos, _atr_signals())
        assert result is not None
        assert result.verdict == Verdict.sell
        assert result.rule_label == RULE_KEY_SELL_EXTENSION_ATR

    def test_well_above_threshold_triggers_sell(self):
        pos = FakePosition(current_price=200.0)  # ratio = 100
        result = rule_sell_extension_atr(pos, _atr_signals())
        assert result is not None
        assert result.verdict == Verdict.sell

    def test_below_trim_threshold_does_not_trigger(self):
        # ratio = 7.99 — neither rule fires
        pos = FakePosition(current_price=107.99)
        assert rule_sell_extension_atr(pos, _atr_signals()) is None

    def test_missing_sma_does_not_trigger(self):
        pos = FakePosition(current_price=200.0)
        assert rule_sell_extension_atr(pos, _atr_signals(sma_50=None)) is None

    def test_missing_or_zero_atr_does_not_trigger(self):
        pos = FakePosition(current_price=200.0)
        assert rule_sell_extension_atr(pos, _atr_signals(atr_14=None)) is None
        assert rule_sell_extension_atr(pos, _atr_signals(atr_14=0.0)) is None

    def test_custom_sell_threshold_via_params(self):
        # raise sell threshold to 12 → ratio=10 no longer triggers sell
        pos = FakePosition(current_price=110.0)
        params = default_extension_atr_params() | {"sell_threshold": 12.0}
        assert rule_sell_extension_atr(pos, _atr_signals(), params) is None


class TestExtensionRulesIntegration:
    """End-to-end checks that extension rules play nice with evaluate_position."""

    def _selections(self):
        return [
            StrategyRuleSelection(
                RULE_KEY_SELL_EXTENSION_ATR,
                params=default_extension_atr_params(),
            ),
            StrategyRuleSelection(
                RULE_KEY_TRIM_EXTENSION_ATR,
                params=default_extension_atr_params(),
            ),
            StrategyRuleSelection(RULE_KEY_HOLD_ABOVE_COST),
        ]

    def test_sell_wins_when_both_rules_fire(self):
        pos = FakePosition(current_price=110.0)
        results = evaluate_position(pos, signals=_atr_signals(), configured_rules=self._selections())
        # Both Trim and Sell fired plus Hold; Sell should win precedence.
        assert results, "expected at least one rule to fire"
        assert results[0].verdict == Verdict.sell
        assert results[0].rule_label == RULE_KEY_SELL_EXTENSION_ATR
        # Trim should also be in the triggered list at lower priority.
        labels = [r.rule_label for r in results]
        assert RULE_KEY_TRIM_EXTENSION_ATR in labels

    def test_only_trim_fires_when_between_thresholds(self):
        pos = FakePosition(current_price=109.0)  # ratio = 9
        results = evaluate_position(pos, signals=_atr_signals(), configured_rules=self._selections())
        assert results[0].verdict == Verdict.trim
        assert results[0].rule_label == RULE_KEY_TRIM_EXTENSION_ATR

    def test_neither_extension_rule_fires_below_8x(self):
        pos = FakePosition(current_price=107.0)
        results = evaluate_position(pos, signals=_atr_signals(), configured_rules=self._selections())
        # Only HOLD-ABOVE-COST should fire
        assert [r.rule_label for r in results] == [RULE_KEY_HOLD_ABOVE_COST]


class TestExtensionIndicatorRequirements:
    def test_defaults(self):
        sma_req, atr_req = get_extension_indicator_requirements(None)
        assert sma_req == ("daily", 50)
        assert atr_req == ("daily", 14)

    def test_uses_params(self):
        sma_req, atr_req = get_extension_indicator_requirements(
            {"interval": "weekly", "sma_period": 200, "atr_period": 21}
        )
        assert sma_req == ("weekly", 200)
        assert atr_req == ("weekly", 21)

    def test_invalid_interval_falls_back_to_default(self):
        sma_req, _ = get_extension_indicator_requirements({"interval": "monthly"})
        assert sma_req == ("daily", 50)

    def test_invalid_periods_fall_back_to_defaults(self):
        sma_req, atr_req = get_extension_indicator_requirements(
            {"sma_period": "fifty", "atr_period": -1}
        )
        assert sma_req == ("daily", 50)
        assert atr_req == ("daily", 14)


# ---------------------------------------------------------------------------
# Tests for rule_trim_weekly_upper_wick (issue #19)
# ---------------------------------------------------------------------------


def _wick_bar(d, o, h, l, c):
    return WeeklyOhlcBar(bar_date=d, open=o, high=h, low=l, close=c)


class TestRuleTrimWeeklyUpperWick:
    def _signals_with_history(self, bars):
        s = MarketSignals()
        s.weekly_ohlc_history = bars
        return s

    def test_trims_on_classic_upper_wick_near_high(self):
        # tight range; close 5% below high; small body; long upper wick
        # range=8, body=1 (12.5%), upper_wick=5 (62.5%), close 5% below recent_high=100
        latest = _wick_bar(date(2024, 6, 7), o=94, h=100, l=92, c=95)
        prev = _wick_bar(date(2024, 5, 31), o=90, h=98, l=88, c=95)
        signals = self._signals_with_history([latest, prev])
        result = rule_trim_weekly_upper_wick(FakePosition(), signals)
        assert result is not None
        assert result.verdict == Verdict.trim
        assert result.rule_label == RULE_KEY_TRIM_WEEKLY_UPPER_WICK

    def test_no_trim_when_close_far_from_recent_high(self):
        # Wick + body OK on latest bar, but recent_high (from older bar) is
        # far above close (95) so the "near recent highs" filter rejects it.
        latest = _wick_bar(date(2024, 6, 7), o=94, h=100, l=92, c=95)
        old_high = _wick_bar(date(2024, 1, 5), o=200, h=300, l=200, c=250)
        signals = self._signals_with_history([latest, old_high])
        result = rule_trim_weekly_upper_wick(FakePosition(), signals)
        assert result is None

    def test_no_trim_when_body_too_large(self):
        # body = 60 of 100 range = 60% > 25% max (use permissive near-high
        # so we isolate the body-ratio failure)
        latest = _wick_bar(date(2024, 6, 7), o=100, h=200, l=100, c=160)
        signals = self._signals_with_history([latest])
        result = rule_trim_weekly_upper_wick(
            FakePosition(),
            signals,
            params={"near_high_pct": 50.0, "upper_wick_ratio_min": 0.30},
        )
        assert result is None

    def test_no_trim_when_upper_wick_too_small(self):
        # upper wick = 20 of 100 range = 20% < 60% min (use permissive
        # near-high so we isolate the wick-ratio failure)
        latest = _wick_bar(date(2024, 6, 7), o=100, h=200, l=100, c=180)
        signals = self._signals_with_history([latest])
        result = rule_trim_weekly_upper_wick(
            FakePosition(),
            signals,
            params={"near_high_pct": 50.0},
        )
        assert result is None

    def test_no_trim_on_zero_range_bar(self):
        latest = _wick_bar(date(2024, 6, 7), o=100, h=100, l=100, c=100)
        signals = self._signals_with_history([latest])
        assert rule_trim_weekly_upper_wick(FakePosition(), signals) is None

    def test_returns_none_when_ohlc_missing(self):
        latest = WeeklyOhlcBar(bar_date=date(2024, 6, 7), open=None, high=200, low=100, close=110)
        signals = self._signals_with_history([latest])
        assert rule_trim_weekly_upper_wick(FakePosition(), signals) is None

    def test_returns_none_when_history_empty(self):
        assert rule_trim_weekly_upper_wick(FakePosition(), MarketSignals()) is None

    def test_custom_params_override_thresholds(self):
        # range=8, body=1 (12.5%), upper_wick=5 (62.5%), close 5% below high=100.
        # With defaults this fires; tighten to require 70% wick → no longer fires.
        latest = _wick_bar(date(2024, 6, 7), o=94, h=100, l=92, c=95)
        signals = self._signals_with_history([latest])
        # Defaults: triggers
        assert rule_trim_weekly_upper_wick(FakePosition(), signals) is not None
        # Custom: 70% wick min — should not trigger (62.5% < 70%)
        result = rule_trim_weekly_upper_wick(
            FakePosition(),
            signals,
            params={
                "upper_wick_ratio_min": 0.70,
                "body_ratio_max": 0.25,
                "near_high_pct": 5.0,
                "lookback_high_weeks": 4,
            },
        )
        assert result is None

    def test_lookback_window_bounds_recent_high(self):
        # Old very-high week is outside lookback window of 2; recent_high = 100
        latest = _wick_bar(date(2024, 6, 7), o=94, h=100, l=92, c=95)
        prev = _wick_bar(date(2024, 5, 31), o=90, h=98, l=88, c=95)
        old_high = _wick_bar(date(2024, 1, 5), o=200, h=500, l=200, c=400)
        signals = self._signals_with_history([latest, prev, old_high])
        # Default lookback=26 includes the old very-high → no trim
        assert rule_trim_weekly_upper_wick(FakePosition(), signals) is None
        # Lookback=2 excludes it → trim fires
        result = rule_trim_weekly_upper_wick(
            FakePosition(),
            signals,
            params={"lookback_high_weeks": 2},
        )
        assert result is not None

    def test_default_upper_wick_params_values(self):
        params = default_upper_wick_params()
        assert params["upper_wick_ratio_min"] == 0.60
        assert params["body_ratio_max"] == 0.25
        assert params["near_high_pct"] == 5.0
        assert params["lookback_high_weeks"] == 26

    def test_get_upper_wick_lookback_weeks_uses_default(self):
        assert get_upper_wick_lookback_weeks(None) == 26
        assert get_upper_wick_lookback_weeks({}) == 26
        assert get_upper_wick_lookback_weeks({"lookback_high_weeks": "x"}) == 26
        assert get_upper_wick_lookback_weeks({"lookback_high_weeks": 13}) == 13


# ---------------------------------------------------------------------------
# Tests for distribution-cluster rules (issue #20)
# ---------------------------------------------------------------------------


def _vol_bar(d, o, c, v):
    """Helper: weekly bar with explicit open/close + volume."""
    return WeeklyOhlcBar(bar_date=d, open=o, high=max(o, c), low=min(o, c), close=c, volume=v)


def _signals_with(bars):
    s = MarketSignals()
    s.weekly_ohlc_history = bars
    return s


class TestDistributionClusterRules:
    def _build_history(self, recent_red_count, recent_red_volume=200.0):
        """Build a history with `recent_red_count` high-volume red weeks
        in the most recent 8 weeks, plus 20 normal red+green weeks of
        baseline median volume = 100.
        """
        history = []
        # Most recent: high-volume red weeks
        d = date(2025, 6, 13)
        for i in range(recent_red_count):
            history.append(_vol_bar(d, o=100, c=90, v=recent_red_volume))
            d -= timedelta(weeks=1)
        # Fill remaining cluster window with green weeks (no hits)
        for _ in range(8 - recent_red_count):
            history.append(_vol_bar(d, o=90, c=100, v=100.0))
            d -= timedelta(weeks=1)
        # Baseline: normal red weeks at vol 100 → median = 100
        for _ in range(12):
            history.append(_vol_bar(d, o=100, c=90, v=100.0))
            d -= timedelta(weeks=1)
        return history

    def test_two_hits_triggers_trim(self):
        # Defaults: trim_hits=2, sell_hits=3, multiplier=1.5 → threshold=150
        # 2 hits at vol 200 in last 8 weeks → trim
        signals = _signals_with(self._build_history(recent_red_count=2))
        result = rule_trim_distribution_cluster(FakePosition(), signals)
        assert result is not None
        assert result.verdict == Verdict.trim
        # Sell rule should not fire at 2 hits
        assert rule_sell_distribution_cluster(FakePosition(), signals) is None

    def test_three_hits_triggers_sell_not_trim(self):
        # 3 hits at vol 200 → sell fires; trim defers (>= sell_hits)
        signals = _signals_with(self._build_history(recent_red_count=3))
        sell = rule_sell_distribution_cluster(FakePosition(), signals)
        assert sell is not None
        assert sell.verdict == Verdict.sell
        trim = rule_trim_distribution_cluster(FakePosition(), signals)
        assert trim is None

    def test_one_hit_does_nothing(self):
        signals = _signals_with(self._build_history(recent_red_count=1))
        assert rule_trim_distribution_cluster(FakePosition(), signals) is None
        assert rule_sell_distribution_cluster(FakePosition(), signals) is None

    def test_high_volume_green_weeks_ignored(self):
        # 5 high-volume GREEN weeks in cluster window → no hits
        history = []
        d = date(2025, 6, 13)
        for _ in range(5):
            history.append(_vol_bar(d, o=90, c=100, v=300.0))  # green high-vol
            d -= timedelta(weeks=1)
        for _ in range(20):  # baseline of normal red weeks
            history.append(_vol_bar(d, o=100, c=90, v=100.0))
            d -= timedelta(weeks=1)
        signals = _signals_with(history)
        assert rule_trim_distribution_cluster(FakePosition(), signals) is None
        assert rule_sell_distribution_cluster(FakePosition(), signals) is None

    def test_returns_none_when_no_red_baseline(self):
        # All-green baseline → no median to compute
        history = []
        d = date(2025, 6, 13)
        for _ in range(8):
            history.append(_vol_bar(d, o=100, c=90, v=300.0))  # red but no baseline
            d -= timedelta(weeks=1)
        for _ in range(20):
            history.append(_vol_bar(d, o=90, c=100, v=100.0))  # all green baseline
            d -= timedelta(weeks=1)
        signals = _signals_with(history)
        # baseline window is just history[:20] which contains 8 red + 12 green.
        # The 8 red weeks form the baseline, vol=300, median=300 → threshold=450
        # No hit weeks reach 450. So no fire.
        assert rule_trim_distribution_cluster(FakePosition(), signals) is None

    def test_missing_volume_skips_bars(self):
        # Bar with volume=None should not be counted
        history = [
            WeeklyOhlcBar(bar_date=date(2025, 6, 13), open=100, high=100, low=90, close=90, volume=None),
            WeeklyOhlcBar(bar_date=date(2025, 6, 6), open=100, high=100, low=90, close=90, volume=None),
        ] + [
            _vol_bar(date(2025, 5, 30) - timedelta(weeks=i), o=100, c=90, v=100.0)
            for i in range(20)
        ]
        signals = _signals_with(history)
        # No volume on recent bars → no hits, so no trim/sell
        assert rule_trim_distribution_cluster(FakePosition(), signals) is None
        assert rule_sell_distribution_cluster(FakePosition(), signals) is None

    def test_returns_none_on_empty_history(self):
        signals = _signals_with([])
        assert rule_trim_distribution_cluster(FakePosition(), signals) is None
        assert rule_sell_distribution_cluster(FakePosition(), signals) is None

    def test_custom_params_override(self):
        # 2 hits at 200 vol; raise trim_hits to 5 → no trim fires
        signals = _signals_with(self._build_history(recent_red_count=2))
        result = rule_trim_distribution_cluster(
            FakePosition(),
            signals,
            params={"trim_hits": 5, "sell_hits": 6},
        )
        assert result is None

    def test_default_distribution_cluster_params_values(self):
        params = default_distribution_cluster_params()
        assert params["baseline_lookback_weeks"] == 20
        assert params["cluster_window_weeks"] == 8
        assert params["volume_multiplier"] == 1.5
        assert params["trim_hits"] == 2
        assert params["sell_hits"] == 3

    def test_get_distribution_cluster_lookback_weeks_uses_max(self):
        assert get_distribution_cluster_lookback_weeks(None) == 20
        assert get_distribution_cluster_lookback_weeks(
            {"baseline_lookback_weeks": 10, "cluster_window_weeks": 30}
        ) == 30
        assert get_distribution_cluster_lookback_weeks(
            {"baseline_lookback_weeks": 50, "cluster_window_weeks": 4}
        ) == 50


# ---------------------------------------------------------------------------
# Tests for lower-high / lower-low rules (issue #21)
# ---------------------------------------------------------------------------


def _hl_bar(d, h, l):
    return WeeklyOhlcBar(bar_date=d, open=(h + l) / 2, high=h, low=l, close=(h + l) / 2)


class TestLowerHighLowerLowRules:
    def _bars_from_pattern(self, highs_lows):
        """Build a most-recent-first weekly bar history from a chronological
        list of (high, low) tuples.
        """
        d = date(2025, 6, 13)
        bars_chrono = []
        for i, (h, l) in enumerate(highs_lows):
            bars_chrono.append(_hl_bar(d - timedelta(weeks=len(highs_lows) - 1 - i), h, l))
        return list(reversed(bars_chrono))

    def test_uptrend_then_first_lower_high_triggers_trim(self):
        # Pivot 1/1. Pattern produces a lower swing high without forming a
        # confirmed lower swing low afterward.
        # bar:   0    1    2    3    4    5    6    7    8    9
        # high: 100  120  100   90  115  100   85   95   90   95
        # low:   95  115   95   80  110   95   80   90   85   88
        # swing highs: idx 1 (120), idx 4 (115), idx 7 (95) → 95 < 115 (LH)
        # swing lows:  idx 3 (80), idx 6 (80) → equal, not strictly lower
        pattern = [
            (100, 95), (120, 115), (100, 95), (90, 80), (115, 110),
            (100, 95), (85, 80), (95, 90), (90, 85), (95, 88),
        ]
        bars = self._bars_from_pattern(pattern)
        signals = _signals_with(bars)
        result = rule_trim_first_lower_high(
            FakePosition(),
            signals,
            params={"pivot_left": 1, "pivot_right": 1, "require_prior_uptrend": False},
        )
        assert result is not None
        assert result.verdict == Verdict.trim
        assert result.rule_label == RULE_KEY_TRIM_FIRST_LOWER_HIGH
        # Sell rule must NOT fire here (no confirmed lower low)
        assert rule_sell_lower_high_lower_low(
            FakePosition(),
            signals,
            params={"pivot_left": 1, "pivot_right": 1, "require_prior_uptrend": False},
        ) is None

    def test_lower_high_plus_lower_low_triggers_sell(self):
        # Pivot 1/1; build LH then LL after:
        # bar:     0    1    2    3    4    5    6    7    8    9
        # high:    100  130  110  90   125  100  90   80   95   85
        # low:     95   125  105  85   120  95   85   70   90   80
        # swing highs: idx 1 (130), idx 4 (125) → LH
        # swing lows:  idx 3 (85), idx 7 (70) → LL after the LH
        pattern = [
            (100, 95), (130, 125), (110, 105), (90, 85), (125, 120),
            (100, 95), (90, 85), (80, 70), (95, 90), (85, 80),
        ]
        bars = self._bars_from_pattern(pattern)
        signals = _signals_with(bars)
        result = rule_sell_lower_high_lower_low(
            FakePosition(),
            signals,
            params={"pivot_left": 1, "pivot_right": 1, "require_prior_uptrend": False},
        )
        assert result is not None
        assert result.verdict == Verdict.sell
        assert result.rule_label == RULE_KEY_SELL_LOWER_HIGH_LOWER_LOW
        # Trim rule should NOT also fire when sell pattern is confirmed
        assert rule_trim_first_lower_high(
            FakePosition(),
            signals,
            params={"pivot_left": 1, "pivot_right": 1, "require_prior_uptrend": False},
        ) is None

    def test_choppy_data_does_not_false_trigger(self):
        # Sideways oscillation: alternating bars that don't form clear trend
        pattern = [(100, 90)] * 20
        bars = self._bars_from_pattern(pattern)
        signals = _signals_with(bars)
        assert rule_trim_first_lower_high(FakePosition(), signals) is None
        assert rule_sell_lower_high_lower_low(FakePosition(), signals) is None

    def test_insufficient_history_returns_none(self):
        bars = [_hl_bar(date(2025, 6, 13), 100, 90)]
        signals = _signals_with(bars)
        assert rule_trim_first_lower_high(FakePosition(), signals) is None
        assert rule_sell_lower_high_lower_low(FakePosition(), signals) is None

    def test_empty_history_returns_none(self):
        signals = _signals_with([])
        assert rule_trim_first_lower_high(FakePosition(), signals) is None
        assert rule_sell_lower_high_lower_low(FakePosition(), signals) is None

    def test_missing_high_low_skips_pivot(self):
        # All bars missing high → no pivots → no trigger
        bars = [
            WeeklyOhlcBar(bar_date=date(2025, 6, 13) - timedelta(weeks=i),
                          open=None, high=None, low=None, close=None)
            for i in range(15)
        ]
        signals = _signals_with(bars)
        assert rule_trim_first_lower_high(FakePosition(), signals) is None
        assert rule_sell_lower_high_lower_low(FakePosition(), signals) is None

    def test_default_lh_ll_params_values(self):
        params = default_lh_ll_params()
        assert params["pivot_left"] == 2
        assert params["pivot_right"] == 2
        assert params["lookback_weeks"] == 30
        assert params["require_prior_uptrend"] is True

    def test_get_lh_ll_lookback_weeks_uses_default(self):
        assert get_lh_ll_lookback_weeks(None) == 30
        assert get_lh_ll_lookback_weeks({"lookback_weeks": 50}) == 50
        assert get_lh_ll_lookback_weeks({}) == 30

    def test_require_prior_uptrend_filters_non_uptrend_lower_highs(self):
        # Two declining swing highs but no prior uptrend leading up to them.
        # require_prior_uptrend=True (default) → no trim
        pattern = [
            (110, 100), (100, 90), (90, 80),  # declining only — no uptrend
            (95, 85), (90, 80), (85, 75), (80, 70),
            (85, 75), (80, 70), (75, 65),
        ]
        bars = self._bars_from_pattern(pattern)
        signals = _signals_with(bars)
        # With require_prior_uptrend=False the rule may fire; with True it
        # should suppress.
        assert rule_trim_first_lower_high(
            FakePosition(), signals, params={"require_prior_uptrend": True}
        ) is None


# ---------------------------------------------------------------------------
# Tests for relative-weakness vs sector rule (issue #22)
# ---------------------------------------------------------------------------


from app.rule_engine import (  # noqa: E402
    DailyClosePoint,
    RULE_KEY_TRIM_RELATIVE_WEAKNESS,
    default_relative_weakness_params,
    get_relative_weakness_lookback_days,
    rule_trim_relative_weakness_vs_sector,
)


def _daily_history(closes: list[float]) -> list[DailyClosePoint]:
    """Build a daily-close history (most-recent first) from a list of prices.

    closes[0] is treated as the most-recent close.
    """
    today = date(2026, 4, 24)
    return [
        DailyClosePoint(bar_date=today - timedelta(days=i), close=c)
        for i, c in enumerate(closes)
    ]


def _signals_with_daily(history_by_ticker: dict[str, list[float]]) -> MarketSignals:
    return MarketSignals(
        daily_close_history={
            t.upper(): _daily_history(closes) for t, closes in history_by_ticker.items()
        }
    )


class TestRelativeWeaknessVsSector:
    def test_triggers_when_benchmark_up_and_stock_lags(self):
        # Stock: 100 → 102 (+2%), Benchmark SMH: 100 → 112 (+12%)
        # Lookback = 5 days.  Gap = 10 → meets default 10% gap.
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        signals = _signals_with_daily({
            "NVDA": [102, 101.5, 101, 100.5, 100.2, 100],
            "SMH": [112, 110, 108, 105, 102, 100],
        })
        result = rule_trim_relative_weakness_vs_sector(
            pos, signals, params={"lookback_days": 5}
        )
        assert result is not None
        assert result.verdict == Verdict.trim
        assert result.rule_label == RULE_KEY_TRIM_RELATIVE_WEAKNESS
        assert "SMH" in result.description

    def test_no_trigger_when_benchmark_flat(self):
        # Benchmark up only 2% → below 8% min_benchmark_return default
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        signals = _signals_with_daily({
            "NVDA": [90, 92, 95, 97, 99, 100],   # stock down 10%
            "SMH": [102, 101, 101, 100, 100, 100],  # benchmark up 2%
        })
        assert rule_trim_relative_weakness_vs_sector(
            pos, signals, params={"lookback_days": 5}
        ) is None

    def test_no_trigger_when_gap_below_threshold(self):
        # Benchmark +12, stock +5 → gap 7% < default 10%
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        signals = _signals_with_daily({
            "NVDA": [105, 104, 103, 102, 101, 100],
            "SMH": [112, 110, 108, 105, 102, 100],
        })
        assert rule_trim_relative_weakness_vs_sector(
            pos, signals, params={"lookback_days": 5}
        ) is None

    def test_skips_when_no_benchmark_configured(self):
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker=None)
        signals = _signals_with_daily({
            "NVDA": [102, 101.5, 101, 100.5, 100.2, 100],
            "SMH": [112, 110, 108, 105, 102, 100],
        })
        assert rule_trim_relative_weakness_vs_sector(
            pos, signals, params={"lookback_days": 5}
        ) is None

    def test_skips_when_benchmark_history_missing(self):
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        signals = _signals_with_daily({
            "NVDA": [102, 101.5, 101, 100.5, 100.2, 100],
            # No SMH history
        })
        assert rule_trim_relative_weakness_vs_sector(
            pos, signals, params={"lookback_days": 5}
        ) is None

    def test_skips_when_stock_history_missing(self):
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        signals = _signals_with_daily({"SMH": [112, 110, 108, 105, 102, 100]})
        assert rule_trim_relative_weakness_vs_sector(
            pos, signals, params={"lookback_days": 5}
        ) is None

    def test_skips_when_history_too_short(self):
        # Need lookback_days + 1 bars
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        signals = _signals_with_daily({
            "NVDA": [102, 100],   # only 2 bars, need 6
            "SMH": [112, 100],
        })
        assert rule_trim_relative_weakness_vs_sector(
            pos, signals, params={"lookback_days": 5}
        ) is None

    def test_uses_custom_thresholds(self):
        # Benchmark +5%, stock -3% → gap 8%.
        # Defaults would skip (benchmark < 8%).  Custom thresholds let it fire.
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        signals = _signals_with_daily({
            "NVDA": [97, 98, 99, 100, 100, 100],
            "SMH": [105, 104, 103, 102, 101, 100],
        })
        result = rule_trim_relative_weakness_vs_sector(
            pos, signals,
            params={
                "lookback_days": 5,
                "min_benchmark_return": 4.0,
                "underperformance_gap": 7.0,
            },
        )
        assert result is not None
        assert result.verdict == Verdict.trim

    def test_uses_default_lookback_when_missing(self):
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        signals = MarketSignals()  # no history
        # Should be skipped without crashing on missing data
        assert rule_trim_relative_weakness_vs_sector(pos, signals, params=None) is None

    def test_default_params_helper(self):
        params = default_relative_weakness_params()
        assert params["lookback_days"] == 63
        assert params["min_benchmark_return"] == 8.0
        assert params["underperformance_gap"] == 10.0

    def test_get_lookback_days_helper(self):
        assert get_relative_weakness_lookback_days(None) == 63
        assert get_relative_weakness_lookback_days({"lookback_days": 30}) == 30
        assert get_relative_weakness_lookback_days({}) == 63
        # Invalid types fall back to default
        assert get_relative_weakness_lookback_days({"lookback_days": -1}) == 63
        assert get_relative_weakness_lookback_days({"lookback_days": "x"}) == 63

    def test_invalid_close_in_history_is_handled(self):
        # Mixed None + valid closes — should still compute when enough valid
        pos = FakePosition(ticker="NVDA", sector_benchmark_ticker="SMH")
        history_with_nones = [
            DailyClosePoint(bar_date=date(2026, 4, 24) - timedelta(days=i), close=c)
            for i, c in enumerate([102, None, 101, None, 100.5, 100.2, 100])
        ]
        signals = MarketSignals(daily_close_history={
            "NVDA": history_with_nones,
            "SMH": _daily_history([112, 110, 108, 105, 102, 100]),
        })
        # Position needs lookback+1=6 valid closes.  We have 5 valid → skip.
        assert rule_trim_relative_weakness_vs_sector(
            pos, signals, params={"lookback_days": 5}
        ) is None

    def test_evaluator_integration_via_evaluate_position(self):
        # Wire through evaluate_position — should trigger trim when enabled
        pos = FakePosition(
            ticker="NVDA",
            sector_benchmark_ticker="SMH",
            current_price=102.0,
            cost_basis=100.0,
        )
        signals = _signals_with_daily({
            "NVDA": [102, 101.5, 101, 100.5, 100.2, 100],
            "SMH": [112, 110, 108, 105, 102, 100],
        })
        results = evaluate_position(
            pos,
            signals=signals,
            configured_rules=[
                StrategyRuleSelection(
                    RULE_KEY_TRIM_RELATIVE_WEAKNESS,
                    params={"lookback_days": 5},
                ),
            ],
        )
        assert any(r.rule_label == RULE_KEY_TRIM_RELATIVE_WEAKNESS for r in results)


# ---------------------------------------------------------------------------
# Tests for failed-breakout / reclaim-failure rule (issue #23)
# ---------------------------------------------------------------------------


@dataclass
class FakeKeyLevel:
    level_price: float
    is_active: bool = True
    label: str | None = None
    notes: str | None = None


def _make_weekly_bars(closes_and_highs: list[tuple[float, float]]) -> list[WeeklyOhlcBar]:
    """Build weekly bars (most-recent first) from a list of (close, high) tuples
    given in chronological order.

    The list passed in is chronological [oldest...newest]; we reverse so the
    resulting MarketSignals.weekly_ohlc_history is most-recent first.
    """
    chronological = []
    base_date = date(2025, 1, 3)  # Friday
    for i, (close, high) in enumerate(closes_and_highs):
        chronological.append(WeeklyOhlcBar(
            bar_date=base_date + timedelta(weeks=i),
            open=close,
            high=high,
            low=min(close, high) - 1,
            close=close,
            volume=1000.0,
        ))
    return list(reversed(chronological))


def _signals_with_weekly(bars: list[WeeklyOhlcBar]) -> MarketSignals:
    return MarketSignals(weekly_ohlc_history=bars)


class TestFailedBreakoutReclaim:
    def test_full_sequence_triggers_sell(self):
        # Level = 100. Chronologically:
        #   weeks 1-3: closes 95,96,97 (below)
        #   week 4: 102 close, high 102 — breakout (confirm_weeks=1)
        #   week 5: 95 close (failure: <= 99)
        #   week 6: high 100, close 95 — failed reclaim
        bars = _make_weekly_bars([
            (95, 96), (96, 97), (97, 98),
            (102, 103), (95, 102), (95, 100),
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = [FakeKeyLevel(level_price=100.0, label="LTH")]

        result = rule_sell_failed_breakout_reclaim(pos, signals)
        assert result is not None
        assert result.verdict == Verdict.sell
        assert result.rule_label == RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM
        assert "LTH" in result.description

    def test_breakout_without_failure_no_trigger(self):
        # Breakout at 102 then stays above
        bars = _make_weekly_bars([
            (95, 96), (102, 103), (105, 107), (108, 110), (110, 112),
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = [FakeKeyLevel(level_price=100.0)]
        assert rule_sell_failed_breakout_reclaim(pos, signals) is None

    def test_failure_with_successful_reclaim_no_trigger(self):
        # Breakout, failure, then a successful reclaim (close above level)
        bars = _make_weekly_bars([
            (95, 96), (102, 103), (95, 102), (105, 106),  # successful reclaim
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = [FakeKeyLevel(level_price=100.0)]
        assert rule_sell_failed_breakout_reclaim(pos, signals) is None

    def test_no_key_levels_no_trigger(self):
        bars = _make_weekly_bars([
            (95, 96), (102, 103), (95, 102), (95, 100),
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = []
        assert rule_sell_failed_breakout_reclaim(pos, signals) is None

    def test_inactive_key_level_skipped(self):
        bars = _make_weekly_bars([
            (95, 96), (102, 103), (95, 102), (95, 100),
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = [FakeKeyLevel(level_price=100.0, is_active=False)]
        assert rule_sell_failed_breakout_reclaim(pos, signals) is None

    def test_reclaim_attempt_outside_window_no_trigger(self):
        # reclaim_window_weeks=4 default — failed reclaim attempt happens
        # 5 weeks after the failure, so it's outside the window.
        bars = _make_weekly_bars([
            (102, 103),                             # breakout
            (95, 102),                              # failure
            (90, 92), (90, 92), (90, 92), (90, 92), # 4 quiet bars below
            (95, 100),                              # would-be failed reclaim, but outside window
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = [FakeKeyLevel(level_price=100.0)]
        assert rule_sell_failed_breakout_reclaim(pos, signals) is None

    def test_no_weekly_history_no_trigger(self):
        signals = MarketSignals()  # empty history
        pos = FakePosition()
        pos.key_levels = [FakeKeyLevel(level_price=100.0)]
        assert rule_sell_failed_breakout_reclaim(pos, signals) is None

    def test_multiple_levels_lowest_evaluated_first(self):
        # Two levels: 100 (matches) and 200 (no signal).  Lowest first.
        bars = _make_weekly_bars([
            (95, 96), (102, 103), (95, 102), (95, 100),
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = [
            FakeKeyLevel(level_price=200.0, label="HIGH"),
            FakeKeyLevel(level_price=100.0, label="LOW"),
        ]
        result = rule_sell_failed_breakout_reclaim(pos, signals)
        assert result is not None
        assert "LOW" in result.description

    def test_confirm_weeks_requires_consecutive_breakout(self):
        # Only 1 week above level, but confirm_weeks=2 → no breakout confirmed
        bars = _make_weekly_bars([
            (95, 96), (102, 103), (95, 102), (95, 100),
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = [FakeKeyLevel(level_price=100.0)]
        result = rule_sell_failed_breakout_reclaim(
            pos, signals, params={"breakout_confirm_weeks": 2}
        )
        assert result is None

    def test_default_params_helper(self):
        params = default_failed_breakout_params()
        assert params["breakout_confirm_weeks"] == 1
        assert params["failure_buffer_pct"] == 1.0
        assert params["reclaim_window_weeks"] == 4
        assert params["reclaim_fail_buffer_pct"] == 0.5
        assert params["lookback_weeks"] == 52

    def test_get_lookback_helper(self):
        assert get_failed_breakout_lookback_weeks(None) == 52
        assert get_failed_breakout_lookback_weeks({"lookback_weeks": 100}) == 100
        assert get_failed_breakout_lookback_weeks({}) == 52

    def test_evaluator_integration_via_evaluate_position(self):
        bars = _make_weekly_bars([
            (95, 96), (102, 103), (95, 102), (95, 100),
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition(current_price=95.0)
        pos.key_levels = [FakeKeyLevel(level_price=100.0)]
        results = evaluate_position(
            pos,
            signals=signals,
            configured_rules=[
                StrategyRuleSelection(RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM),
            ],
        )
        assert any(r.rule_label == RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM for r in results)
        assert get_verdict(results) == Verdict.sell

    def test_failure_buffer_pct_respects_threshold(self):
        # Level 100, failure_buffer_pct = 5 → failure threshold = 95
        # Close at 96 should NOT trigger failure.
        bars = _make_weekly_bars([
            (95, 96), (102, 103), (96, 102), (96, 100),
        ])
        signals = _signals_with_weekly(bars)
        pos = FakePosition()
        pos.key_levels = [FakeKeyLevel(level_price=100.0)]
        assert rule_sell_failed_breakout_reclaim(
            pos, signals, params={"failure_buffer_pct": 5.0}
        ) is None
