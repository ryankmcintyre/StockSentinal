"""
Rule Engine for Stock Investment Decision Assistant.

All sell/trim/hold rule logic lives here, isolated from routes and UI.
Rules are pure functions that take a Position-like object and return a RuleResult.
Rules are evaluated in priority order; the highest-priority triggered rule wins.
"""

import json
from dataclasses import dataclass, field
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

    Fixed fields are kept for backward compatibility.  The flexible
    ``ma_signals`` dict stores arbitrary MA indicator data keyed by
    ``(interval, time_period)`` → ``(close_value, sma_value)``.
    """
    daily_close: Optional[float] = None
    daily_sma_21: Optional[float] = None
    weekly_close: Optional[float] = None
    weekly_sma_20: Optional[float] = None

    # Flexible MA signal store populated from the indicator cache.
    # Key: (interval, time_period)  Value: (close_value, sma_value)
    ma_signals: dict[tuple[str, int], tuple[Optional[float], Optional[float]]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class StrategyRuleSelection:
    """Persisted user selection for a rule within a strategy."""
    rule_key: str
    params: Optional[dict] = None  # parsed params_json


@dataclass(frozen=True)
class RuleSpec:
    """Catalog metadata + evaluator for a selectable rule."""
    key: str
    name: str
    description: str
    verdict: Verdict
    supported_investment_types: tuple[InvestmentType, ...]
    default_sort_order: int
    evaluator: Callable[[PositionLike, MarketSignals, Optional[dict]], Optional[RuleResult]]


RULE_KEY_SELL_MA_ALL = "SELL_MA_ALL"
RULE_KEY_TRIM_10PCT = "TRIM-10PCT"
RULE_KEY_HOLD_ABOVE_COST = "HOLD-ABOVE-COST"

VERDICT_PRIORITY = {Verdict.sell: 0, Verdict.trim: 1, Verdict.hold: 2}

# Maximum number of MA conditions per investment type
MAX_MA_CONDITIONS = 10


# ---------------------------------------------------------------------------
# Individual rule functions
# Each rule returns a RuleResult if it triggers, or None if it doesn't apply.
# ---------------------------------------------------------------------------


def rule_sell_ma_all(
    position: PositionLike,
    signals: MarketSignals,
    params: Optional[dict] = None,
) -> Optional[RuleResult]:
    """SELL if ALL configured MA conditions are met.

    Each condition specifies an interval ('daily'/'weekly') and a
    time_period (2..200).  For Sell to trigger, every condition must
    have data available and the close must be below the SMA.  If any
    condition is missing data, that condition is treated as not met
    (Sell does not trigger).

    params shape:
        {"conditions": [{"interval": "weekly", "time_period": 20}, ...]}
    """
    if params is None:
        return None
    conditions = params.get("conditions", [])
    if not conditions:
        return None

    descriptions: list[str] = []
    for cond in conditions:
        interval = cond.get("interval")
        time_period = cond.get("time_period")
        if interval is None or time_period is None:
            return None  # malformed condition → not met

        signal = signals.ma_signals.get((interval, time_period))
        if signal is None:
            return None  # missing data → not met
        close_val, sma_val = signal
        if close_val is None or sma_val is None:
            return None  # missing data → not met
        if close_val >= sma_val:
            return None  # condition not met → Sell does not trigger

        descriptions.append(
            f"{interval.capitalize()} close ({close_val:.2f}) < "
            f"SMA-{time_period} ({sma_val:.2f})"
        )

    return RuleResult(
        rule_label=RULE_KEY_SELL_MA_ALL,
        verdict=Verdict.sell,
        description="; ".join(descriptions),
    )


def rule_trim_above_10_percent(
    position: PositionLike,
    _signals: Optional[MarketSignals] = None,
    _params: Optional[dict] = None,
) -> Optional[RuleResult]:
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


def rule_hold_above_cost_basis(
    position: PositionLike,
    _signals: Optional[MarketSignals] = None,
    _params: Optional[dict] = None,
) -> Optional[RuleResult]:
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


def _eval_sell_ma(
    position: PositionLike, signals: MarketSignals, params: Optional[dict] = None,
) -> Optional[RuleResult]:
    return rule_sell_ma_all(position, signals, params)


def _eval_trim(
    position: PositionLike, signals: MarketSignals, params: Optional[dict] = None,
) -> Optional[RuleResult]:
    return rule_trim_above_10_percent(position, signals, params)


def _eval_hold(
    position: PositionLike, signals: MarketSignals, params: Optional[dict] = None,
) -> Optional[RuleResult]:
    return rule_hold_above_cost_basis(position, signals, params)


RULE_CATALOG: dict[str, RuleSpec] = {
    RULE_KEY_SELL_MA_ALL: RuleSpec(
        key=RULE_KEY_SELL_MA_ALL,
        name="Sell on moving average conditions",
        description="Sell when ALL configured MA conditions are below their averages",
        verdict=Verdict.sell,
        supported_investment_types=(InvestmentType.long_term, InvestmentType.short_term),
        default_sort_order=10,
        evaluator=_eval_sell_ma,
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

# Default sell MA conditions per investment type
DEFAULT_MA_CONDITIONS: dict[InvestmentType, list[dict]] = {
    InvestmentType.long_term: [{"interval": "weekly", "time_period": 20}],
    InvestmentType.short_term: [{"interval": "daily", "time_period": 21}],
}

DEFAULT_RULE_SELECTIONS: dict[InvestmentType, list[StrategyRuleSelection]] = {
    InvestmentType.long_term: [
        StrategyRuleSelection(
            RULE_KEY_SELL_MA_ALL,
            params={"conditions": DEFAULT_MA_CONDITIONS[InvestmentType.long_term]},
        ),
        StrategyRuleSelection(RULE_KEY_TRIM_10PCT),
        StrategyRuleSelection(RULE_KEY_HOLD_ABOVE_COST),
    ],
    InvestmentType.short_term: [
        StrategyRuleSelection(
            RULE_KEY_SELL_MA_ALL,
            params={"conditions": DEFAULT_MA_CONDITIONS[InvestmentType.short_term]},
        ),
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
    return list(DEFAULT_RULE_SELECTIONS[normalized])


# ---------------------------------------------------------------------------
# MA condition validation helpers
# ---------------------------------------------------------------------------


def validate_ma_conditions(conditions: list[dict]) -> list[str]:
    """Validate a list of MA conditions.  Returns a list of error messages (empty if valid)."""
    errors: list[str] = []
    if len(conditions) > MAX_MA_CONDITIONS:
        errors.append(f"Maximum {MAX_MA_CONDITIONS} conditions allowed")

    seen: set[tuple[str, int]] = set()
    valid_intervals = {"daily", "weekly"}

    for i, cond in enumerate(conditions):
        interval = cond.get("interval")
        time_period = cond.get("time_period")

        if interval not in valid_intervals:
            errors.append(f"Condition {i + 1}: interval must be 'daily' or 'weekly'")
            continue

        if not isinstance(time_period, int) or time_period < 2 or time_period > 200:
            errors.append(f"Condition {i + 1}: time_period must be an integer between 2 and 200")
            continue

        key = (interval, time_period)
        if key in seen:
            errors.append(f"Condition {i + 1}: duplicate ({interval}, {time_period})")
        seen.add(key)

    return errors


def parse_params_json(raw: Optional[str]) -> Optional[dict]:
    """Parse a params_json string into a dict.  Returns None on invalid or non-object JSON."""
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(parsed, dict):
        return None

    return parsed
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

    candidates: list[tuple[int, int, str, RuleSpec, Optional[dict]]] = []
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
                selection.params,
            )
        )

    candidates.sort(key=lambda row: (row[0], row[1], row[2]))

    for _, _, _, spec, params in candidates:
        result = spec.evaluator(position, signals, params)
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
