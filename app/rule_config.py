"""Persistence helpers for strategy rule configuration."""

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import StrategyRuleConfig
from app.rule_engine import (
    RULE_CATALOG,
    StrategyRuleSelection,
    default_rule_selections_for_investment_type,
    list_rule_specs_for_investment_type,
)
from app.schemas import InvestmentType


def _supported_investment_types() -> tuple[InvestmentType, InvestmentType]:
    return (InvestmentType.long_term, InvestmentType.short_term)


def _normalize_investment_type(value: str) -> InvestmentType:
    try:
        return InvestmentType(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported investment type '{value}'") from exc


def ensure_strategy_rule_defaults(db: Session) -> None:
    """Seed missing rule configuration rows for each investment type.

    Existing rows are preserved. New catalog rules are added as disabled by
    default unless they are part of the strategy defaults.
    """
    changed = False

    for investment_type in _supported_investment_types():
        existing_rows = (
            db.query(StrategyRuleConfig)
            .filter(StrategyRuleConfig.investment_type == investment_type.value)
            .all()
        )
        existing_by_key = {row.rule_key: row for row in existing_rows}

        # Derive defaults from the rule engine's single source of truth.
        default_enabled_keys = {
            s.rule_key for s in default_rule_selections_for_investment_type(investment_type)
        }

        for spec in list_rule_specs_for_investment_type(investment_type):
            if spec.key in existing_by_key:
                continue

            db.add(
                StrategyRuleConfig(
                    investment_type=investment_type.value,
                    rule_key=spec.key,
                    enabled=spec.key in default_enabled_keys,
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
            StrategyRuleSelection(rule_key=row.rule_key)
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
            rules.append(
                {
                    "rule_key": spec.key,
                    "name": spec.name,
                    "description": spec.description,
                    "verdict": spec.verdict.value,
                    "enabled": bool(config.enabled) if config is not None else False,
                }
            )

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

    row.enabled = enabled
    row.updated_at = datetime.now()
    db.commit()
