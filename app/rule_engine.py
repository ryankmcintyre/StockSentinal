"""
Rule Engine for Stock Investment Decision Assistant.

All sell/trim/hold rule logic lives here, isolated from routes and UI.
Rules are pure functions that take a Position-like object and return a RuleResult.
Rules are evaluated in priority order; the highest-priority triggered rule wins.
"""

from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional, Protocol

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


@dataclass(frozen=True)
class StrategyRuleSelection:
    """Persisted user selection for a rule within a strategy."""
    rule_key: str


@dataclass(frozen=True)
class RuleSpec:
    """Catalog metadata + evaluator for a selectable rule."""
    key: str
    name: str
    description: str
    verdict: Verdict
    supported_investment_types: tuple[InvestmentType, ...]
    default_sort_order: int
    evaluator: Callable[[PositionLike, MarketSignals], Optional[RuleResult]]


RULE_KEY_LT_SELL_20W_MA = "LT-SELL-20W-MA"
RULE_KEY_ST_SELL_21D_MA = "ST-SELL-21D-MA"
RULE_KEY_TRIM_10PCT = "TRIM-10PCT"
RULE_KEY_HOLD_ABOVE_COST = "HOLD-ABOVE-COST"

VERDICT_PRIORITY = {Verdict.sell: 0, Verdict.trim: 1, Verdict.hold: 2}


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
            rule_label=RULE_KEY_LT_SELL_20W_MA,
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
            rule_label=RULE_KEY_ST_SELL_21D_MA,
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
            rule_label=RULE_KEY_TRIM_10PCT,
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
            rule_label=RULE_KEY_HOLD_ABOVE_COST,
            verdict=Verdict.hold,
            description="Price is at or above cost basis",
        )
    return None


# ---------------------------------------------------------------------------
# Rule catalog + defaults
# ---------------------------------------------------------------------------


def _eval_lt_sell(position: PositionLike, signals: MarketSignals) -> Optional[RuleResult]:
    return rule_long_term_sell_below_20w_ma(position, signals)


def _eval_st_sell(position: PositionLike, signals: MarketSignals) -> Optional[RuleResult]:
    return rule_short_term_sell_below_21d_ma(position, signals)


def _eval_trim(position: PositionLike, _: MarketSignals) -> Optional[RuleResult]:
    return rule_trim_above_10_percent(position)


def _eval_hold(position: PositionLike, _: MarketSignals) -> Optional[RuleResult]:
    return rule_hold_above_cost_basis(position)


RULE_CATALOG: dict[str, RuleSpec] = {
    RULE_KEY_LT_SELL_20W_MA: RuleSpec(
        key=RULE_KEY_LT_SELL_20W_MA,
        name="Long-term sell below 20-week MA",
        description="Weekly close is below the 20-week moving average",
        verdict=Verdict.sell,
        supported_investment_types=(InvestmentType.long_term,),
        default_sort_order=10,
        evaluator=_eval_lt_sell,
    ),
    RULE_KEY_ST_SELL_21D_MA: RuleSpec(
        key=RULE_KEY_ST_SELL_21D_MA,
        name="Short-term sell below 21-day MA",
        description="Daily close is below the 21-day moving average",
        verdict=Verdict.sell,
        supported_investment_types=(InvestmentType.short_term,),
        default_sort_order=10,
        evaluator=_eval_st_sell,
    ),
    RULE_KEY_TRIM_10PCT: RuleSpec(
        key=RULE_KEY_TRIM_10PCT,
        name="Trim above 10% gain",
        description="Price is more than 10% above cost basis",
        verdict=Verdict.trim,
        supported_investment_types=(InvestmentType.long_term, InvestmentType.short_term),
        default_sort_order=20,
        evaluator=_eval_trim,
    ),
    RULE_KEY_HOLD_ABOVE_COST: RuleSpec(
        key=RULE_KEY_HOLD_ABOVE_COST,
        name="Hold at or above cost basis",
        description="Price is at or above cost basis",
        verdict=Verdict.hold,
        supported_investment_types=(InvestmentType.long_term, InvestmentType.short_term),
        default_sort_order=30,
        evaluator=_eval_hold,
    ),
}

DEFAULT_RULE_SELECTIONS: dict[InvestmentType, list[StrategyRuleSelection]] = {
    InvestmentType.long_term: [
        StrategyRuleSelection(RULE_KEY_LT_SELL_20W_MA),
        StrategyRuleSelection(RULE_KEY_TRIM_10PCT),
        StrategyRuleSelection(RULE_KEY_HOLD_ABOVE_COST),
    ],
    InvestmentType.short_term: [
        StrategyRuleSelection(RULE_KEY_ST_SELL_21D_MA),
        StrategyRuleSelection(RULE_KEY_TRIM_10PCT),
        StrategyRuleSelection(RULE_KEY_HOLD_ABOVE_COST),
    ],
}


def _normalize_investment_type(value: str | InvestmentType) -> Optional[InvestmentType]:
    """Normalize string/enum investment types to an InvestmentType enum."""
    if isinstance(value, InvestmentType):
        return value
    try:
        return InvestmentType(value)
    except ValueError:
        return None


def list_rule_specs_for_investment_type(investment_type: str | InvestmentType) -> list[RuleSpec]:
    """Return all catalog rules that can be applied to a given strategy."""
    normalized = _normalize_investment_type(investment_type)
    if normalized is None:
        return []
    specs = [spec for spec in RULE_CATALOG.values() if normalized in spec.supported_investment_types]
    specs.sort(
        key=lambda spec: (
            VERDICT_PRIORITY.get(spec.verdict, 99),
            spec.default_sort_order,
            spec.key,
        )
    )
    return specs


def default_rule_selections_for_investment_type(
    investment_type: str | InvestmentType,
) -> list[StrategyRuleSelection]:
    """Return default enabled rule selections for an investment type."""
    normalized = _normalize_investment_type(investment_type)
    if normalized is None:
        return []
    return [StrategyRuleSelection(s.rule_key) for s in DEFAULT_RULE_SELECTIONS[normalized]]


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
    configured_rules: Optional[list[StrategyRuleSelection]] = None,
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
        configured_rules: Optional user-selected rules for the position's
            strategy. When None, defaults are used.
    """
    if signals is None:
        signals = MarketSignals()

    triggered: list[RuleResult] = []
    investment_type = _normalize_investment_type(position.investment_type)
    if investment_type is None:
        return triggered

    selections = configured_rules
    if selections is None:
        selections = default_rule_selections_for_investment_type(investment_type)

    candidates: list[tuple[int, int, str, RuleSpec]] = []
    seen_rule_keys: set[str] = set()
    for selection in selections:
        if selection.rule_key in seen_rule_keys:
            continue
        seen_rule_keys.add(selection.rule_key)

        spec = RULE_CATALOG.get(selection.rule_key)
        if spec is None:
            continue
        if investment_type not in spec.supported_investment_types:
            continue
        candidates.append(
            (
                VERDICT_PRIORITY.get(spec.verdict, 99),
                spec.default_sort_order,
                spec.key,
                spec,
            )
        )

    candidates.sort(key=lambda row: (row[0], row[1], row[2]))

    for _, _, _, spec in candidates:
        result = spec.evaluator(position, signals)
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
