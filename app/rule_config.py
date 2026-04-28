"""Persistence helpers for strategy rule configuration."""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import StrategyRuleConfig
from app.rule_engine import (
    RULE_CATALOG,
    RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
    RULE_KEY_SELL_EXTENSION_ATR,
    RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM,
    RULE_KEY_SELL_LOWER_HIGH_LOWER_LOW,
    RULE_KEY_SELL_MA_ALL,
    RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
    RULE_KEY_TRIM_EXTENSION_ATR,
    RULE_KEY_TRIM_FIRST_LOWER_HIGH,
    RULE_KEY_TRIM_RELATIVE_WEAKNESS,
    RULE_KEY_TRIM_WEEKLY_UPPER_WICK,
    StrategyRuleSelection,
    default_distribution_cluster_params,
    default_extension_atr_params,
    default_failed_breakout_params,
    default_lh_ll_params,
    default_relative_weakness_params,
    default_rule_selections_for_investment_type,
    default_upper_wick_params,
    get_distribution_cluster_lookback_weeks,
    get_extension_indicator_requirements,
    get_failed_breakout_lookback_weeks,
    get_lh_ll_lookback_weeks,
    get_relative_weakness_lookback_days,
    get_upper_wick_lookback_weeks,
    list_rule_specs_for_investment_type,
    parse_params_json,
    validate_ma_conditions,
)
from app.schemas import InvestmentType


def _supported_investment_types() -> tuple[InvestmentType, InvestmentType]:
    return (InvestmentType.long_term, InvestmentType.short_term)


def _normalize_investment_type(value: str) -> InvestmentType:
    try:
        return InvestmentType(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported investment type '{value}'") from exc


# Old rule keys replaced by SELL_MA_ALL — removed during migration
_DEPRECATED_SELL_RULE_KEYS = {"LT-SELL-20W-MA", "ST-SELL-21D-MA"}


def _migrate_deprecated_sell_rules(db: Session) -> bool:
    """Remove deprecated hardcoded sell rule rows, replaced by SELL_MA_ALL."""
    changed = False
    for old_key in _DEPRECATED_SELL_RULE_KEYS:
        rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.rule_key == old_key)
            .all()
        )
        for row in rows:
            db.delete(row)
            changed = True
    return changed


def ensure_strategy_rule_defaults(db: Session) -> None:
    """Seed missing rule configuration rows for each investment type.

    Existing rows are preserved. New catalog rules are added as disabled by
    default unless they are part of the strategy defaults. Deprecated sell
    rule keys are cleaned up.
    """
    changed = _migrate_deprecated_sell_rules(db)

    for investment_type in _supported_investment_types():
        existing_rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .all()
        )
        existing_by_key = {row.rule_key: row for row in existing_rows}

        # Derive defaults from the rule engine's single source of truth.
        default_selections = default_rule_selections_for_investment_type(investment_type)
        default_by_key = {s.rule_key: s for s in default_selections}
        default_enabled_keys = set(default_by_key.keys())
        spec_by_key = {spec.key: spec for spec in list_rule_specs_for_investment_type(investment_type)}

        for rule_key, row in existing_by_key.items():
            spec = spec_by_key.get(rule_key)
            if spec is None:
                continue
            if row.sort_order != spec.default_sort_order:
                row.sort_order = spec.default_sort_order
                changed = True

        for spec in spec_by_key.values():
            if spec.key in existing_by_key:
                continue

            default_sel = default_by_key.get(spec.key)
            params_json = None
            if default_sel and default_sel.params:
                params_json = json.dumps(default_sel.params)
            elif spec.key in (RULE_KEY_TRIM_EXTENSION_ATR, RULE_KEY_SELL_EXTENSION_ATR):
                # Seed sensible defaults for the ATR-extension rules even
                # when they are disabled by default so the UI/refresh
                # pipeline can reason about their thresholds and indicator
                # requirements without first requiring user configuration.
                params_json = json.dumps(default_extension_atr_params())
            elif spec.key == RULE_KEY_TRIM_WEEKLY_UPPER_WICK:
                # Seed defaults for the upper-wick rule so the rules UI
                # and weekly-bar lookback calculation know its parameters
                # immediately on first run.
                params_json = json.dumps(default_upper_wick_params())
            elif spec.key in (
                RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
                RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
            ):
                # Seed defaults for the distribution-cluster rules so the
                # rules UI and weekly-bar lookback calculation know their
                # baseline + cluster window parameters immediately.
                params_json = json.dumps(default_distribution_cluster_params())
            elif spec.key in (
                RULE_KEY_TRIM_FIRST_LOWER_HIGH,
                RULE_KEY_SELL_LOWER_HIGH_LOWER_LOW,
            ):
                # Seed defaults for the lower-high / lower-low rules so the
                # rules UI and weekly-bar lookback calculation know their
                # pivot + lookback parameters immediately.
                params_json = json.dumps(default_lh_ll_params())
            elif spec.key == RULE_KEY_TRIM_RELATIVE_WEAKNESS:
                # Seed defaults for the relative-weakness rule so the rules
                # UI and daily-bar lookback calculation know its parameters
                # immediately on first run.
                params_json = json.dumps(default_relative_weakness_params())
            elif spec.key == RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM:
                # Seed defaults for the failed-breakout rule so the rules
                # UI and weekly-bar lookback know its parameters
                # immediately on first run.
                params_json = json.dumps(default_failed_breakout_params())

            db.add(
                StrategyRuleConfig(
                    investment_type=investment_type.value,
                    rule_key=spec.key,
                    enabled=spec.key in default_enabled_keys,
                    sort_order=spec.default_sort_order,
                    params_json=params_json,
                )
            )
            changed = True

    if changed:
        db.commit()


def get_enabled_rule_selections_by_investment_type(
    db: Session,
) -> dict[str, list[StrategyRuleSelection]]:
    """Return enabled rule selections keyed by investment type value."""
    ensure_strategy_rule_defaults(db)

    selections: dict[str, list[StrategyRuleSelection]] = {}
    for investment_type in _supported_investment_types():
        rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .all()
        )
        selections[investment_type.value] = [
            StrategyRuleSelection(
                rule_key=row.rule_key,
                params=parse_params_json(row.params_json),
            )
            for row in rows
        ]
    return selections


def get_rule_management_sections(db: Session) -> list[dict]:
    """Build template-ready rule management sections for long/short strategies."""
    ensure_strategy_rule_defaults(db)

    sections: list[dict] = []
    for investment_type in _supported_investment_types():
        rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .all()
        )
        rows_by_key = {row.rule_key: row for row in rows}

        rules = []
        for spec in list_rule_specs_for_investment_type(investment_type):
            config = rows_by_key.get(spec.key)
            rule_data: dict = {
                "rule_key": spec.key,
                "name": spec.name,
                "description": spec.description,
                "verdict": spec.verdict.value,
                "enabled": bool(config.enabled) if config is not None else False,
            }

            # Include MA conditions for SELL_MA_ALL
            if spec.key == RULE_KEY_SELL_MA_ALL and config is not None:
                params = parse_params_json(config.params_json)
                conditions = params.get("conditions", []) if params else []
                rule_data["ma_conditions"] = conditions

            # Include configured thresholds for the ATR-extension rules
            if spec.key in (RULE_KEY_TRIM_EXTENSION_ATR, RULE_KEY_SELL_EXTENSION_ATR) and config is not None:
                default_params = default_extension_atr_params()
                parsed_params = parse_params_json(config.params_json)
                params = parsed_params if parsed_params is not None else default_params
                threshold_key = (
                    "trim_threshold"
                    if spec.key == RULE_KEY_TRIM_EXTENSION_ATR
                    else "sell_threshold"
                )
                default_threshold = default_params[threshold_key]
                raw_threshold = params.get(threshold_key)
                try:
                    threshold = float(raw_threshold)
                    if threshold <= 0:
                        threshold = default_threshold
                except (TypeError, ValueError):
                    threshold = default_threshold

                sanitized_params = dict(params)
                sanitized_params[threshold_key] = threshold

                (interval, sma_period), (_, atr_period) = get_extension_indicator_requirements(
                    sanitized_params
                )
                rule_data["extension_params"] = {
                    "threshold": threshold,
                    "sma_period": sma_period,
                    "atr_period": atr_period,
                    "interval": interval,
                }

            # Include configured thresholds for the weekly upper-wick rule
            if spec.key == RULE_KEY_TRIM_WEEKLY_UPPER_WICK and config is not None:
                params = parse_params_json(config.params_json) or default_upper_wick_params()
                rule_data["upper_wick_params"] = {
                    "upper_wick_ratio_min": params.get(
                        "upper_wick_ratio_min", default_upper_wick_params()["upper_wick_ratio_min"]
                    ),
                    "body_ratio_max": params.get(
                        "body_ratio_max", default_upper_wick_params()["body_ratio_max"]
                    ),
                    "near_high_pct": params.get(
                        "near_high_pct", default_upper_wick_params()["near_high_pct"]
                    ),
                    "lookback_high_weeks": get_upper_wick_lookback_weeks(params),
                }

            # Include configured thresholds for the distribution-cluster rules
            if spec.key in (
                RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
                RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
            ) and config is not None:
                defaults = default_distribution_cluster_params()
                params = parse_params_json(config.params_json) or defaults
                hits_key = (
                    "trim_hits"
                    if spec.key == RULE_KEY_TRIM_DISTRIBUTION_CLUSTER
                    else "sell_hits"
                )
                rule_data["distribution_cluster_params"] = {
                    "baseline_lookback_weeks": params.get(
                        "baseline_lookback_weeks", defaults["baseline_lookback_weeks"]
                    ),
                    "cluster_window_weeks": params.get(
                        "cluster_window_weeks", defaults["cluster_window_weeks"]
                    ),
                    "volume_multiplier": params.get(
                        "volume_multiplier", defaults["volume_multiplier"]
                    ),
                    "hits_required": params.get(hits_key, defaults[hits_key]),
                    "hits_kind": (
                        "trim" if spec.key == RULE_KEY_TRIM_DISTRIBUTION_CLUSTER else "sell"
                    ),
                }

            # Include configured thresholds for the lower-high / lower-low rules
            if spec.key in (
                RULE_KEY_TRIM_FIRST_LOWER_HIGH,
                RULE_KEY_SELL_LOWER_HIGH_LOWER_LOW,
            ) and config is not None:
                defaults = default_lh_ll_params()
                params = parse_params_json(config.params_json) or defaults
                rule_data["lh_ll_params"] = {
                    "pivot_left": params.get("pivot_left", defaults["pivot_left"]),
                    "pivot_right": params.get("pivot_right", defaults["pivot_right"]),
                    "lookback_weeks": get_lh_ll_lookback_weeks(params),
                    "require_prior_uptrend": params.get(
                        "require_prior_uptrend", defaults["require_prior_uptrend"]
                    ),
                    "kind": (
                        "trim" if spec.key == RULE_KEY_TRIM_FIRST_LOWER_HIGH else "sell"
                    ),
                }

            # Include configured thresholds for the relative-weakness rule
            if spec.key == RULE_KEY_TRIM_RELATIVE_WEAKNESS and config is not None:
                defaults = default_relative_weakness_params()
                params = parse_params_json(config.params_json) or defaults
                rule_data["relative_weakness_params"] = {
                    "lookback_days": get_relative_weakness_lookback_days(params),
                    "min_benchmark_return": params.get(
                        "min_benchmark_return", defaults["min_benchmark_return"]
                    ),
                    "underperformance_gap": params.get(
                        "underperformance_gap", defaults["underperformance_gap"]
                    ),
                }

            # Include configured thresholds for the failed-breakout rule
            if spec.key == RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM and config is not None:
                defaults = default_failed_breakout_params()
                params = parse_params_json(config.params_json) or defaults
                rule_data["failed_breakout_params"] = {
                    "breakout_confirm_weeks": params.get(
                        "breakout_confirm_weeks", defaults["breakout_confirm_weeks"]
                    ),
                    "failure_buffer_pct": params.get(
                        "failure_buffer_pct", defaults["failure_buffer_pct"]
                    ),
                    "reclaim_window_weeks": params.get(
                        "reclaim_window_weeks", defaults["reclaim_window_weeks"]
                    ),
                    "reclaim_fail_buffer_pct": params.get(
                        "reclaim_fail_buffer_pct", defaults["reclaim_fail_buffer_pct"]
                    ),
                    "lookback_weeks": get_failed_breakout_lookback_weeks(params),
                }

            rules.append(rule_data)

        title = "Long-term strategy rules" if investment_type == InvestmentType.long_term else "Short-term strategy rules"
        sections.append(
            {
                "investment_type": investment_type.value,
                "title": title,
                "rules": rules,
            }
        )

    return sections


def update_strategy_rule_config(
    db: Session,
    investment_type_value: str,
    rule_key: str,
    enabled: bool,
) -> None:
    """Upsert a strategy-rule selection row."""
    investment_type = _normalize_investment_type(investment_type_value)
    spec = RULE_CATALOG.get(rule_key)
    if spec is None:
        raise ValueError(f"Unsupported rule key '{rule_key}'")
    if investment_type not in spec.supported_investment_types:
        raise ValueError(f"Rule '{rule_key}' is not valid for {investment_type.value}")

    ensure_strategy_rule_defaults(db)
    row = (
        db.query(StrategyRuleConfig)
        .filter(StrategyRuleConfig.investment_type == investment_type.value)
        .filter(StrategyRuleConfig.rule_key == rule_key)
        .first()
    )
    if row is None:
        row = StrategyRuleConfig(
            investment_type=investment_type.value,
            rule_key=rule_key,
            sort_order=spec.default_sort_order,
        )
        db.add(row)

    # SELL_MA_ALL requires at least one condition to be enabled.
    if enabled and rule_key == RULE_KEY_SELL_MA_ALL:
        params = parse_params_json(row.params_json)
        conditions = params.get("conditions", []) if params else []
        if not conditions:
            raise ValueError(
                f"Cannot enable '{rule_key}' for {investment_type.value}: "
                "at least one MA condition is required"
            )

    row.enabled = enabled
    row.updated_at = datetime.now()
    db.commit()


# ---------------------------------------------------------------------------
# MA condition CRUD helpers
# ---------------------------------------------------------------------------


def get_ma_conditions(db: Session, investment_type_value: str) -> list[dict]:
    """Return the MA conditions for SELL_MA_ALL for a given investment type."""
    ensure_strategy_rule_defaults(db)
    row = (
        db.query(StrategyRuleConfig)
        .filter(StrategyRuleConfig.investment_type == investment_type_value)
        .filter(StrategyRuleConfig.rule_key == RULE_KEY_SELL_MA_ALL)
        .first()
    )
    if row is None or row.params_json is None:
        return []
    params = parse_params_json(row.params_json)
    return params.get("conditions", []) if params else []


def add_ma_condition(
    db: Session,
    investment_type_value: str,
    interval: str,
    time_period: int,
) -> list[str]:
    """Add an MA condition.  Returns list of validation errors (empty on success)."""
    _normalize_investment_type(investment_type_value)
    ensure_strategy_rule_defaults(db)

    row = (
        db.query(StrategyRuleConfig)
        .filter(StrategyRuleConfig.investment_type == investment_type_value)
        .filter(StrategyRuleConfig.rule_key == RULE_KEY_SELL_MA_ALL)
        .first()
    )
    if row is None:
        return ["SELL_MA_ALL rule not found"]

    params = parse_params_json(row.params_json) or {}
    conditions = list(params.get("conditions", []))

    new_cond = {"interval": interval, "time_period": time_period}
    conditions.append(new_cond)

    errors = validate_ma_conditions(conditions)
    if errors:
        return errors

    params["conditions"] = conditions
    row.params_json = json.dumps(params)
    row.updated_at = datetime.now()
    db.commit()
    return []


def remove_ma_condition(
    db: Session,
    investment_type_value: str,
    interval: str,
    time_period: int,
) -> list[str]:
    """Remove an MA condition by identity (interval, time_period).

    Returns list of validation errors (empty on success).
    At least one condition must remain when the rule is enabled.
    """
    _normalize_investment_type(investment_type_value)
    ensure_strategy_rule_defaults(db)

    row = (
        db.query(StrategyRuleConfig)
        .filter(StrategyRuleConfig.investment_type == investment_type_value)
        .filter(StrategyRuleConfig.rule_key == RULE_KEY_SELL_MA_ALL)
        .first()
    )
    if row is None:
        return ["SELL_MA_ALL rule not found"]

    params = parse_params_json(row.params_json) or {}
    conditions = list(params.get("conditions", []))

    new_conditions = [
        c for c in conditions
        if not (c.get("interval") == interval and c.get("time_period") == time_period)
    ]

    if len(new_conditions) == len(conditions):
        return [f"Condition ({interval}, {time_period}) not found"]

    if not new_conditions and row.enabled:
        return ["Cannot remove the last condition while the rule is enabled"]

    params["conditions"] = new_conditions
    row.params_json = json.dumps(params)
    row.updated_at = datetime.now()
    db.commit()
    return []


def get_required_indicators(db: Session) -> set[tuple[str, int]]:
    """Return the set of (interval, time_period) tuples required by enabled rules.

    Includes indicators required by:
      - SELL_MA_ALL conditions
      - The ATR-extension rules' SMA inputs (e.g. SMA-50 daily)

    Used by the market data refresh logic to know which SMA indicators to fetch.
    """
    ensure_strategy_rule_defaults(db)
    indicators: set[tuple[str, int]] = set()

    for investment_type in _supported_investment_types():
        rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .all()
        )
        for row in rows:
            params = parse_params_json(row.params_json)
            if row.rule_key == RULE_KEY_SELL_MA_ALL:
                if params is None:
                    continue
                for cond in params.get("conditions", []):
                    interval = cond.get("interval")
                    time_period = cond.get("time_period")
                    if interval and isinstance(time_period, int):
                        indicators.add((interval, time_period))
            elif row.rule_key in (RULE_KEY_TRIM_EXTENSION_ATR, RULE_KEY_SELL_EXTENSION_ATR):
                sma_req, _ = get_extension_indicator_requirements(params)
                indicators.add(sma_req)

    return indicators


def get_required_atr_indicators(db: Session) -> set[tuple[str, int]]:
    """Return the set of (interval, time_period) tuples required for ATR data.

    Used by the market data refresh logic to know which ATR indicators to fetch.
    """
    ensure_strategy_rule_defaults(db)
    indicators: set[tuple[str, int]] = set()

    for investment_type in _supported_investment_types():
        rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .filter(
                StrategyRuleConfig.rule_key.in_(
                    (RULE_KEY_TRIM_EXTENSION_ATR, RULE_KEY_SELL_EXTENSION_ATR)
                )
            )
            .all()
        )
        for row in rows:
            params = parse_params_json(row.params_json)
            _, atr_req = get_extension_indicator_requirements(params)
            indicators.add(atr_req)

    return indicators


def get_required_weekly_bar_lookback(db: Session) -> int:
    """Return the largest weekly-OHLC lookback window required by enabled rules.

    Returns 0 when no enabled rule needs weekly OHLC history.
    """
    ensure_strategy_rule_defaults(db)
    max_lookback = 0
    for investment_type in _supported_investment_types():
        rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .filter(
                StrategyRuleConfig.rule_key.in_(
                    (
                        RULE_KEY_TRIM_WEEKLY_UPPER_WICK,
                        RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
                        RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
                        RULE_KEY_TRIM_FIRST_LOWER_HIGH,
                        RULE_KEY_SELL_LOWER_HIGH_LOWER_LOW,
                        RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM,
                    )
                )
            )
            .all()
        )
        for row in rows:
            params = parse_params_json(row.params_json)
            if row.rule_key == RULE_KEY_TRIM_WEEKLY_UPPER_WICK:
                max_lookback = max(max_lookback, get_upper_wick_lookback_weeks(params))
            elif row.rule_key in (
                RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
                RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
            ):
                max_lookback = max(max_lookback, get_distribution_cluster_lookback_weeks(params))
            elif row.rule_key == RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM:
                max_lookback = max(max_lookback, get_failed_breakout_lookback_weeks(params))
            else:
                max_lookback = max(max_lookback, get_lh_ll_lookback_weeks(params))
    return max_lookback


def get_required_daily_bar_lookback(db: Session) -> int:
    """Return the largest daily-close lookback (in trading days) required by enabled rules.

    Returns 0 when no enabled rule needs daily close history.
    """
    ensure_strategy_rule_defaults(db)
    max_lookback = 0
    for investment_type in _supported_investment_types():
        rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .filter(StrategyRuleConfig.rule_key == RULE_KEY_TRIM_RELATIVE_WEAKNESS)
            .all()
        )
        for row in rows:
            params = parse_params_json(row.params_json)
            max_lookback = max(max_lookback, get_relative_weakness_lookback_days(params))
    return max_lookback
