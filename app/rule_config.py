"""Persistence helpers for strategy rule configuration."""

import json
import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

# In-process cache of user IDs that have already had their rule defaults seeded.
# Eliminates the 4 SELECT queries that ensure_strategy_rule_defaults fires on
# every refresh for users whose rows already exist (i.e. essentially everyone
# after the first run).
_SEEDED_USERS: set[str] = set()
_SEEDED_USERS_LOCK = threading.Lock()

from app.models import StrategyRuleConfig
from app.unit_of_work import UnitOfWork
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


def _required_investment_types(investment_type: str | None) -> tuple[InvestmentType, ...]:
    if investment_type is None:
        return _supported_investment_types()
    return (_normalize_investment_type(investment_type),)


def _normalize_investment_type(value: str) -> InvestmentType:
    try:
        return InvestmentType(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported investment type '{value}'") from exc


# Old rule keys replaced by SELL_MA_ALL — removed during migration
_DEPRECATED_SELL_RULE_KEYS = {"LT-SELL-20W-MA", "ST-SELL-21D-MA"}


def _migrate_deprecated_sell_rules(uow: UnitOfWork) -> bool:
    """Remove deprecated hardcoded sell rule rows, replaced by SELL_MA_ALL."""
    changed = False
    for old_key in _DEPRECATED_SELL_RULE_KEYS:
        rows = uow.rule_configs.list_by_key(old_key)
        for row in rows:
            uow.rule_configs.delete(row)
            changed = True
    return changed


def clear_seeded_users_cache() -> None:
    """Clear the in-process seeded-users cache.

    Intended for use in tests where each test runs against a fresh in-memory
    database. Clearing the cache ensures ensure_strategy_rule_defaults re-seeds
    after a database reset, rather than skipping because the prior run's user_id
    is still present.
    """
    with _SEEDED_USERS_LOCK:
        _SEEDED_USERS.clear()


def ensure_strategy_rule_defaults(uow: UnitOfWork, user_id: str | None = None) -> None:
    """Seed missing rule configuration rows for each investment type.

    A valid ``user_id`` is required to seed rows — if it is ``None`` (e.g. an
    unscoped background UoW) seeding is skipped to prevent NULL-owned rows from
    accumulating under the unique constraint.

    Existing rows are preserved. New catalog rules are added as disabled by
    default unless they are part of the strategy defaults. Deprecated sell
    rule keys are cleaned up.

    An in-process ``_SEEDED_USERS`` cache short-circuits the 4 SELECT queries
    that this function fires on every refresh for users whose rows already
    exist. The lock prevents two concurrent threads from racing to seed the
    same brand-new user simultaneously.
    """
    # Per-refresh diagnostic counter — no-op unless REFRESH_PROFILING_ENABLED.
    from app.market_data.profiling import record_seed_invocation
    record_seed_invocation()

    if user_id is None:
        logger.debug("ensure_strategy_rule_defaults called without user_id — skipping seed")
        return

    # Fast path: already seeded in this process — no DB round-trips needed.
    if user_id in _SEEDED_USERS:
        return

    with _SEEDED_USERS_LOCK:
        # Re-check after acquiring the lock (handles concurrent fresh-user race).
        if user_id in _SEEDED_USERS:
            return

        changed = _migrate_deprecated_sell_rules(uow)

        for investment_type in _supported_investment_types():
            existing_rows = uow.rule_configs.list_by_investment_type(investment_type.value)
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

                uow.rule_configs.add(
                    StrategyRuleConfig(
                        user_id=user_id,
                        investment_type=investment_type.value,
                        rule_key=spec.key,
                        enabled=spec.key in default_enabled_keys,
                        sort_order=spec.default_sort_order,
                        params_json=params_json,
                    )
                )
                changed = True

        if changed:
            uow.commit()

        # Add to cache only after a successful commit so a failed seed
        # attempt doesn't permanently skip re-seeding on the next call.
        _SEEDED_USERS.add(user_id)


def get_enabled_rule_selections_by_investment_type(
    uow: UnitOfWork,
    user_id: str | None = None,
    *,
    _skip_defaults: bool = False,
) -> dict[str, list[StrategyRuleSelection]]:
    """Return enabled rule selections keyed by investment type value.

    Uses a single database query to fetch all enabled rules across both
    investment types, then groups them in Python — avoiding two round-trips.
    """
    if not _skip_defaults:
        ensure_strategy_rule_defaults(uow, user_id=user_id)

    all_rows = uow.rule_configs.list_all_enabled()
    rows_by_type: dict[str, list] = {}
    for row in all_rows:
        rows_by_type.setdefault(row.investment_type, []).append(row)

    selections: dict[str, list[StrategyRuleSelection]] = {}
    for investment_type in _supported_investment_types():
        selections[investment_type.value] = [
            StrategyRuleSelection(
                rule_key=row.rule_key,
                params=parse_params_json(row.params_json),
            )
            for row in rows_by_type.get(investment_type.value, [])
        ]
    return selections


def get_rule_management_sections(uow: UnitOfWork, user_id: str | None = None) -> list[dict]:
    """Build template-ready rule management sections for long/short strategies."""
    ensure_strategy_rule_defaults(uow, user_id=user_id)

    sections: list[dict] = []
    for investment_type in _supported_investment_types():
        rows = uow.rule_configs.list_by_investment_type(investment_type.value)
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
    uow: UnitOfWork,
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

    ensure_strategy_rule_defaults(uow, user_id=uow.user_id)
    row = uow.rule_configs.get_by_investment_type_and_key(investment_type.value, rule_key)
    if row is None:
        row = StrategyRuleConfig(
            investment_type=investment_type.value,
            rule_key=rule_key,
            sort_order=spec.default_sort_order,
        )
        uow.rule_configs.add(row)

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
    uow.commit()


# ---------------------------------------------------------------------------
# MA condition CRUD helpers
# ---------------------------------------------------------------------------


def get_ma_conditions(uow: UnitOfWork, investment_type_value: str) -> list[dict]:
    """Return the MA conditions for SELL_MA_ALL for a given investment type."""
    ensure_strategy_rule_defaults(uow, user_id=uow.user_id)
    row = uow.rule_configs.get_by_investment_type_and_key(
        investment_type_value, RULE_KEY_SELL_MA_ALL
    )
    if row is None or row.params_json is None:
        return []
    params = parse_params_json(row.params_json)
    return params.get("conditions", []) if params else []


def add_ma_condition(
    uow: UnitOfWork,
    investment_type_value: str,
    interval: str,
    time_period: int,
) -> list[str]:
    """Add an MA condition.  Returns list of validation errors (empty on success)."""
    _normalize_investment_type(investment_type_value)
    ensure_strategy_rule_defaults(uow, user_id=uow.user_id)

    row = uow.rule_configs.get_by_investment_type_and_key(
        investment_type_value, RULE_KEY_SELL_MA_ALL
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
    uow.commit()
    return []


def remove_ma_condition(
    uow: UnitOfWork,
    investment_type_value: str,
    interval: str,
    time_period: int,
) -> list[str]:
    """Remove an MA condition by identity (interval, time_period).

    Returns list of validation errors (empty on success).
    At least one condition must remain when the rule is enabled.
    """
    _normalize_investment_type(investment_type_value)
    ensure_strategy_rule_defaults(uow, user_id=uow.user_id)

    row = uow.rule_configs.get_by_investment_type_and_key(
        investment_type_value, RULE_KEY_SELL_MA_ALL
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
    uow.commit()
    return []


def get_required_indicators(
    uow: UnitOfWork,
    investment_type: str | None = None,
    *,
    _skip_defaults: bool = False,
) -> set[tuple[str, int]]:
    """Return the set of (interval, time_period) tuples required by enabled rules.

    Includes indicators required by:
      - SELL_MA_ALL conditions
      - The ATR-extension rules' SMA inputs (e.g. SMA-50 daily)

    Used by the market data refresh logic to know which SMA indicators to fetch.
    """
    if not _skip_defaults:
        ensure_strategy_rule_defaults(uow, user_id=uow.user_id)
    indicators: set[tuple[str, int]] = set()

    for required_type in _required_investment_types(investment_type):
        rows = uow.rule_configs.list_enabled_by_investment_type(required_type.value)
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


def get_required_atr_indicators(
    uow: UnitOfWork,
    investment_type: str | None = None,
    *,
    _skip_defaults: bool = False,
) -> set[tuple[str, int]]:
    """Return the set of (interval, time_period) tuples required for ATR data.

    Used by the market data refresh logic to know which ATR indicators to fetch.
    """
    if not _skip_defaults:
        ensure_strategy_rule_defaults(uow, user_id=uow.user_id)
    indicators: set[tuple[str, int]] = set()

    for required_type in _required_investment_types(investment_type):
        rows = uow.rule_configs.list_enabled_by_investment_type_and_keys(
            required_type.value,
            [RULE_KEY_TRIM_EXTENSION_ATR, RULE_KEY_SELL_EXTENSION_ATR],
        )
        for row in rows:
            params = parse_params_json(row.params_json)
            _, atr_req = get_extension_indicator_requirements(params)
            indicators.add(atr_req)

    return indicators


def get_required_weekly_bar_lookback(
    uow: UnitOfWork,
    investment_type: str | None = None,
    *,
    _skip_defaults: bool = False,
) -> int:
    """Return the largest weekly-OHLC lookback window required by enabled rules.

    Returns 0 when no enabled rule needs weekly OHLC history.
    """
    if not _skip_defaults:
        ensure_strategy_rule_defaults(uow, user_id=uow.user_id)
    max_lookback = 0
    for required_type in _required_investment_types(investment_type):
        rows = uow.rule_configs.list_enabled_by_investment_type_and_keys(
            required_type.value,
            [
                RULE_KEY_TRIM_WEEKLY_UPPER_WICK,
                RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
                RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
                RULE_KEY_TRIM_FIRST_LOWER_HIGH,
                RULE_KEY_SELL_LOWER_HIGH_LOWER_LOW,
                RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM,
            ],
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


def get_required_daily_bar_lookback(
    uow: UnitOfWork,
    investment_type: str | None = None,
    *,
    _skip_defaults: bool = False,
) -> int:
    """Return the largest daily-close lookback (in trading days) required by enabled rules.

    Returns 0 when no enabled rule needs daily close history.
    """
    if not _skip_defaults:
        ensure_strategy_rule_defaults(uow, user_id=uow.user_id)
    max_lookback = 0
    for required_type in _required_investment_types(investment_type):
        rows = uow.rule_configs.list_enabled_by_investment_type_and_keys(
            required_type.value,
            [RULE_KEY_TRIM_RELATIVE_WEAKNESS],
        )
        for row in rows:
            params = parse_params_json(row.params_json)
            max_lookback = max(max_lookback, get_relative_weakness_lookback_days(params))
    return max_lookback


class RuleRequirements:
    """All market-data requirements derived from a user's enabled rules.

    Bundles the four ``get_required_*`` outputs into a single object that
    can be produced from one ``list_enabled_by_investment_type`` query per
    investment type, eliminating four redundant per-call queries on the
    refresh hot path.
    """

    __slots__ = (
        "indicators",
        "atr_indicators",
        "weekly_bar_lookback",
        "daily_bar_lookback",
    )

    def __init__(
        self,
        indicators: set[tuple[str, int]],
        atr_indicators: set[tuple[str, int]],
        weekly_bar_lookback: int,
        daily_bar_lookback: int,
    ):
        self.indicators = indicators
        self.atr_indicators = atr_indicators
        self.weekly_bar_lookback = weekly_bar_lookback
        self.daily_bar_lookback = daily_bar_lookback


_WEEKLY_BAR_RULE_KEYS = frozenset(
    {
        RULE_KEY_TRIM_WEEKLY_UPPER_WICK,
        RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
        RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
        RULE_KEY_TRIM_FIRST_LOWER_HIGH,
        RULE_KEY_SELL_LOWER_HIGH_LOWER_LOW,
        RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM,
    }
)


def _compute_requirements_from_rows(
    rows: list,
    required_type_values: set[str],
) -> RuleRequirements:
    """Derive RuleRequirements from a pre-fetched list of enabled rule rows.

    Only rows whose investment_type is in *required_type_values* are
    considered for the requirement calculation.  The caller is responsible
    for passing the correct filter set.
    """
    indicators: set[tuple[str, int]] = set()
    atr_indicators: set[tuple[str, int]] = set()
    weekly_lookback = 0
    daily_lookback = 0

    for row in rows:
        if row.investment_type not in required_type_values:
            continue
        params = parse_params_json(row.params_json)
        key = row.rule_key

        if key == RULE_KEY_SELL_MA_ALL:
            if params is not None:
                for cond in params.get("conditions", []):
                    interval = cond.get("interval")
                    time_period = cond.get("time_period")
                    if interval and isinstance(time_period, int):
                        indicators.add((interval, time_period))
        elif key in (RULE_KEY_TRIM_EXTENSION_ATR, RULE_KEY_SELL_EXTENSION_ATR):
            sma_req, atr_req = get_extension_indicator_requirements(params)
            indicators.add(sma_req)
            atr_indicators.add(atr_req)

        if key in _WEEKLY_BAR_RULE_KEYS:
            if key == RULE_KEY_TRIM_WEEKLY_UPPER_WICK:
                weekly_lookback = max(weekly_lookback, get_upper_wick_lookback_weeks(params))
            elif key in (
                RULE_KEY_TRIM_DISTRIBUTION_CLUSTER,
                RULE_KEY_SELL_DISTRIBUTION_CLUSTER,
            ):
                weekly_lookback = max(
                    weekly_lookback, get_distribution_cluster_lookback_weeks(params)
                )
            elif key == RULE_KEY_SELL_FAILED_BREAKOUT_RECLAIM:
                weekly_lookback = max(
                    weekly_lookback, get_failed_breakout_lookback_weeks(params)
                )
            else:
                weekly_lookback = max(weekly_lookback, get_lh_ll_lookback_weeks(params))

        if key == RULE_KEY_TRIM_RELATIVE_WEAKNESS:
            daily_lookback = max(
                daily_lookback, get_relative_weakness_lookback_days(params)
            )

    return RuleRequirements(
        indicators=indicators,
        atr_indicators=atr_indicators,
        weekly_bar_lookback=weekly_lookback,
        daily_bar_lookback=daily_lookback,
    )


def get_rule_requirements(
    uow: UnitOfWork,
    investment_type: str | None = None,
    *,
    _skip_defaults: bool = False,
) -> RuleRequirements:
    """Compute all four rule-driven market-data requirements in one fetch.

    Replaces four separate ``list_enabled_by_investment_type*`` queries
    with a single ``list_all_enabled`` fetch across investment types and
    derives indicators / ATR indicators / weekly + daily lookbacks in
    Python.
    """
    if not _skip_defaults:
        ensure_strategy_rule_defaults(uow, user_id=uow.user_id)

    required_type_values = {t.value for t in _required_investment_types(investment_type)}
    all_rows = list(uow.rule_configs.list_all_enabled())
    return _compute_requirements_from_rows(all_rows, required_type_values)


def get_rule_requirements_and_selections(
    uow: UnitOfWork,
    user_id: str | None = None,
    investment_type: str | None = None,
    *,
    _skip_defaults: bool = False,
) -> tuple[RuleRequirements, dict[str, list[StrategyRuleSelection]]]:
    """Compute rule requirements AND enabled selections in a single DB fetch.

    On the single-position refresh hot path, ``refresh_position`` previously
    called ``get_rule_requirements`` and ``get_enabled_rule_selections_by_investment_type``
    back-to-back — two separate ``list_all_enabled`` round-trips for the same
    row set.  This helper collapses them into one fetch and derives both
    outputs from the same rows in Python, saving ~63ms per refresh.

    *investment_type* scopes the requirement calculation to a specific
    investment type (as ``get_rule_requirements`` does); pass ``None`` to
    include all types.  The returned selections dict always covers both
    investment types so the caller can pass it directly to ``_enrich_position``.

    Backward-compat: the individual helpers remain available for callers
    that only need one output (e.g. ``_enrich_all_positions`` for selections,
    ``refresh_all`` for requirements).
    """
    if not _skip_defaults:
        ensure_strategy_rule_defaults(uow, user_id=user_id)

    all_rows = list(uow.rule_configs.list_all_enabled())

    # Requirements: filter to the requested investment type(s).
    required_type_values = {t.value for t in _required_investment_types(investment_type)}
    requirements = _compute_requirements_from_rows(all_rows, required_type_values)

    # Selections: group all rows by investment type.
    rows_by_type: dict[str, list] = {}
    for row in all_rows:
        rows_by_type.setdefault(row.investment_type, []).append(row)

    selections: dict[str, list[StrategyRuleSelection]] = {}
    for inv_type in _supported_investment_types():
        selections[inv_type.value] = [
            StrategyRuleSelection(
                rule_key=row.rule_key,
                params=parse_params_json(row.params_json),
            )
            for row in rows_by_type.get(inv_type.value, [])
        ]

    return requirements, selections
