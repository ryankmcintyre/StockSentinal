"""
Rule Engine for Stock Investment Decision Assistant.

All sell/trim/hold rule logic lives here, isolated from routes and UI.
Rules are pure functions that take a Position-like object and return a RuleResult.
Rules are evaluated in priority order; the highest-priority triggered rule wins.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional, Protocol

from app.schemas import InvestmentType, RuleResult, Verdict


class PositionLike(Protocol):
    """Protocol describing the fields the rule engine needs from a position."""
    ticker: str
    cost_basis: float
    current_price: float
    investment_type: str
    initial_purchase_date: date


@dataclass
class MarketSignals:
    """Structured market data consumed by the rule engine.

    All fields are optional so the engine can degrade gracefully when
    Alpha Vantage data is unavailable.
    """
    daily_close: Optional[float] = None
    daily_sma_21: Optional[float] = None
    weekly_close: Optional[float] = None
    weekly_sma_20: Optional[float] = None


# ---------------------------------------------------------------------------
# Individual rule functions
# Each rule returns a RuleResult if it triggers, or None if it doesn't apply.
# ---------------------------------------------------------------------------


def rule_long_term_sell_below_20w_ma(
    position: PositionLike,
    signals: MarketSignals,
) -> Optional[RuleResult]:
    """SELL if a long-term position's weekly close is below the 20-week moving average.

    When market signals are available the comparison is made automatically.
    If weekly close or SMA-20 data is missing the rule cannot fire.
    """
    if position.investment_type != InvestmentType.long_term:
        return None
    if signals.weekly_close is None or signals.weekly_sma_20 is None:
        return None
    if signals.weekly_close < signals.weekly_sma_20:
        return RuleResult(
            rule_label="LT-SELL-20W-MA",
            verdict=Verdict.sell,
            description="Weekly close is below the 20-week moving average",
        )
    return None


def rule_short_term_sell_below_21d_ma(
    position: PositionLike,
    signals: MarketSignals,
) -> Optional[RuleResult]:
    """SELL if a short-term position's daily close is below the 21-day moving average.

    When market signals are available the comparison is made automatically.
    If daily close or SMA-21 data is missing the rule cannot fire.
    """
    if position.investment_type != InvestmentType.short_term:
        return None
    if signals.daily_close is None or signals.daily_sma_21 is None:
        return None
    if signals.daily_close < signals.daily_sma_21:
        return RuleResult(
            rule_label="ST-SELL-21D-MA",
            verdict=Verdict.sell,
            description="Daily close is below the 21-day moving average",
        )
    return None


def rule_trim_above_10_percent(position: PositionLike) -> Optional[RuleResult]:
    """TRIM if the current price is more than 10% above cost basis.

    Applies to both long-term and short-term positions.
    """
    if position.cost_basis <= 0:
        return None
    percent_gain = (position.current_price - position.cost_basis) / position.cost_basis * 100
    if percent_gain > 10:
        return RuleResult(
            rule_label="TRIM-10PCT",
            verdict=Verdict.trim,
            description=f"Price is {percent_gain:.1f}% above cost basis (>10%)",
        )
    return None


def rule_hold_above_cost_basis(position: PositionLike) -> Optional[RuleResult]:
    """HOLD if the current price is at or above the cost basis.

    This is the default hold rule — lowest priority.
    """
    if position.current_price >= position.cost_basis:
        return RuleResult(
            rule_label="HOLD-ABOVE-COST",
            verdict=Verdict.hold,
            description="Price is at or above cost basis",
        )
    return None


# ---------------------------------------------------------------------------
# Computed helpers
# ---------------------------------------------------------------------------


def compute_percent_gain(cost_basis: float, current_price: float) -> float:
    """Calculate percent gain/loss from cost basis."""
    if cost_basis <= 0:
        return 0.0
    return (current_price - cost_basis) / cost_basis * 100


def compute_hold_duration_days(initial_purchase_date: date) -> int:
    """Calculate how many days a position has been held."""
    return (date.today() - initial_purchase_date).days


# ---------------------------------------------------------------------------
# Engine: evaluate a position against the appropriate rule set
# ---------------------------------------------------------------------------


def evaluate_position(
    position: PositionLike,
    signals: Optional[MarketSignals] = None,
) -> list[RuleResult]:
    """Evaluate a position against all applicable rules.

    Rules are checked in priority order (sell → trim → hold).
    Returns a list of all triggered RuleResults. The first item is the
    highest-priority verdict that should be shown to the user.

    Args:
        position: The position to evaluate.
        signals: Cached market data (daily/weekly close and SMA values).
            When None, MA-based sell rules are suppressed and only
            trim/hold rules can fire.
    """
    if signals is None:
        signals = MarketSignals()

    triggered: list[RuleResult] = []

    if position.investment_type == InvestmentType.long_term:
        # Long-term rule set, priority order
        rules = [
            lambda p: rule_long_term_sell_below_20w_ma(p, signals),
            rule_trim_above_10_percent,
            rule_hold_above_cost_basis,
        ]
    elif position.investment_type == InvestmentType.short_term:
        # Short-term rule set, priority order
        rules = [
            lambda p: rule_short_term_sell_below_21d_ma(p, signals),
            rule_trim_above_10_percent,
            rule_hold_above_cost_basis,
        ]
    else:
        return triggered

    for rule_fn in rules:
        result = rule_fn(position)
        if result is not None:
            triggered.append(result)

    return triggered


def get_verdict(triggered_rules: list[RuleResult]) -> Verdict:
    """Return the highest-priority verdict from a list of triggered rules.

    If no rules triggered, defaults to Hold.
    """
    if not triggered_rules:
        return Verdict.hold
    # The first triggered rule is the highest priority
    return triggered_rules[0].verdict
