# Design: User-Configurable Sell Rules (Daily/Weekly MA)

## Goal
Allow the user to define custom **sell** rules for both long-term and short-term positions, where each rule is:
- moving-average based (`daily` or `weekly`)
- configurable by period (`2..200`)

Rules are global for the app (single-user), and the same rule set applies to all positions.

## Confirmed Product Decisions
1. Multiple sell rules are combined with **ALL** logic per investment type.
2. Existing **Trim** and **Hold** behavior stays unchanged.
3. Both long-term and short-term rule sets may include either `daily` or `weekly` rules.
4. MA period bounds are `2..200`.
5. If data for a rule is unavailable, that rule is treated as **not met** (so Sell does not trigger).
6. Rule storage is a single global rule set (not per-position, not per-user profile yet).

## Current State (Gap Analysis)
- Sell logic is hardcoded to:
  - long-term: weekly close < weekly SMA-20
  - short-term: daily close < daily SMA-21
- Position table stores fixed SMA fields (`daily_sma_21`, `weekly_sma_20`) which does not scale to arbitrary periods.
- Rule evaluation has no persisted rule configuration.

## Proposed Architecture

### 1. Data Model Changes
Add a dedicated table for configurable sell rules:

`sell_rules`
- `id` (PK)
- `investment_type` (`long-term` | `short-term`)
- `interval` (`daily` | `weekly`)
- `time_period` (int, constrained to 2..200)
- `sort_order` (int, stable UI ordering)
- `created_at`, `updated_at`

Add a reusable market-data cache table for arbitrary MA periods:

`market_indicator_cache`
- `id` (PK)
- `ticker` (normalized uppercase)
- `interval` (`daily` | `weekly`)
- `time_period` (int)
- `sma_value` (float)
- `sma_date` (date)
- `close_value` (float)  *(same interval/date used for comparison)*
- `close_date` (date)
- `retrieved_at` (datetime)
- unique key on (`ticker`, `interval`, `time_period`)

Why this cache table:
- Supports any MA period without schema changes.
- Reuses one fetch for all positions with same ticker/rule.
- Keeps API usage low and avoids local SMA calculation.

### 2. Rule Engine Changes
Replace hardcoded sell rule functions with data-driven evaluation:

1. Load rule set for the position’s `investment_type`.
2. For each rule, evaluate: `close_value < sma_value`.
3. Sell triggers only when **all** rules evaluate true.
4. If any rule is missing data, that rule is false; Sell does not trigger.
5. If Sell does not trigger, run existing Trim then Hold logic unchanged.

Implementation shape:
- Keep `evaluate_position(...)` entry point.
- Inject rules + signal lookup into evaluation instead of hardcoding 20/21 periods.
- Keep existing `RuleResult` output format so templates do not need major changes.

### 3. Market Data Fetch Strategy (Throttle-Safe)
For each portfolio evaluation/refresh:
1. Build distinct `(ticker, interval, time_period)` needs from positions + configured rules.
2. For each distinct key:
   - reuse fresh cache if available for most recently completed market period.
   - otherwise fetch from Alpha Vantage SMA API (`function=SMA`) and matching series API for close.
3. Persist cache and reuse across all positions.

Important:
- Do **not** compute SMA locally; always use API indicator data.
- Keep existing rate-limit gate (`_wait_for_alpha_vantage_slot`) and dedupe by ticker/rule key.

### 4. UI and Routes
Add a dedicated rules management screen:

`GET /rules`
- Shows two sections:
  - Long-term sell rules
  - Short-term sell rules
- Each row: interval selector + period input (2..200) + delete action.
- Helper text: “All rules in a section must be true to trigger Sell.”

`POST /rules`
- Add rule with validation.

`POST /rules/{id}`
- Update rule values.

`POST /rules/{id}/delete`
- Delete rule only if at least one rule remains for that investment type.

Validation rules:
- Enforce min 1 rule for long-term and min 1 for short-term.
- Prevent invalid periods and intervals.

### 5. Refresh / Manual Override
Extend existing refresh endpoints to support force refresh:
- `POST /refresh?force=true`
- `POST /refresh/{position_id}?force=true`

This keeps cache reuse by default while allowing manual override when needed.

### 6. Migration Plan
1. Create new tables (`sell_rules`, `market_indicator_cache`).
2. Seed defaults if no rules exist:
   - long-term: weekly MA-20
   - short-term: daily MA-21
3. Keep existing position SMA columns temporarily for compatibility.
4. After full cutover, optionally remove fixed SMA columns in a follow-up cleanup.

## Evaluation Example
Short-term rule set:
- daily MA-10
- weekly MA-8

For a short-term position:
- If `daily_close < daily_sma_10` **and** `weekly_close < weekly_sma_8` => Sell
- Otherwise Sell does not trigger; Trim/Hold continue as today.

## Testing Plan
1. Rule engine unit tests:
   - ALL-rule behavior
   - mixed intervals per investment type
   - missing data prevents Sell
2. Route/form tests:
   - add/update/delete rules
   - cannot delete last rule in a section
   - period bounds enforced (2..200)
3. Market data tests:
   - dedupe by `(ticker, interval, time_period)`
   - cache hit avoids API call
   - force refresh bypasses cache
4. Portfolio integration tests:
   - verdicts reflect configured rules while Trim/Hold remain unchanged.

## Risks and Mitigations
- **Higher API volume** with many rules:
  - mitigate via dedupe + cache + existing rate limiter.
- **Inconsistent verdicts during partial refresh failures**:
  - treat missing rule data as rule-not-met and surface refresh error in UI.
- **Schema transition complexity**:
  - keep backward-compatible fields until post-cutover cleanup.

## Out of Scope for This Design
- Per-position custom rule sets
- Multi-user rule ownership
- Non-MA sell conditions (RSI, MACD, etc.)
