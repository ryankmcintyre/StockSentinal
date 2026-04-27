"""Persistence helpers for strategy rule configuration."""

import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import StrategyRuleConfig
from app.rule_engine import (
    RULE_CATALOG,
    RULE_KEY_SELL_MA_ALL,
    StrategyRuleSelection,
    default_rule_selections_for_investment_type,
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

        for spec in list_rule_specs_for_investment_type(investment_type):
            if spec.key in existing_by_key:
                continue

            default_sel = default_by_key.get(spec.key)
            params_json = None
            if default_sel and default_sel.params:
                params_json = json.dumps(default_sel.params)

            db.add(
                StrategyRuleConfig(
                    investment_type=investment_type.value,
                    rule_key=spec.key,
                    enabled=spec.key in default_enabled_keys,
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
    """Return the set of (interval, time_period) tuples required by enabled SELL_MA_ALL rules.

    Used by the market data refresh logic to know which indicators to fetch.
    """
    ensure_strategy_rule_defaults(db)
    indicators: set[tuple[str, int]] = set()

    for investment_type in _supported_investment_types():
        row = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .filter(StrategyRuleConfig.rule_key == RULE_KEY_SELL_MA_ALL)
            .filter(StrategyRuleConfig.enabled.is_(True))
            .first()
        )
        if row is None or row.params_json is None:
            continue
        params = parse_params_json(row.params_json)
        if params is None:
            continue
        for cond in params.get("conditions", []):
            interval = cond.get("interval")
            time_period = cond.get("time_period")
            if interval and isinstance(time_period, int):
                indicators.add((interval, time_period))

    return indicators
