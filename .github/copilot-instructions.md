# Stock Investment Decision Assistant

## Elevator Pitch
A web app that helps investors decide when to sell, trim, or hold stock positions. Users enter long-term and short-term positions, the app fetches market data from a configured provider, and a configurable rule engine produces a clear verdict (Sell / Trim / Hold) for each holding along with the specific rules that triggered it.

## Target Users
- **Multi-user**: Supabase Auth (Google OAuth + email OTP). Each user's positions and rule configs are isolated.
- **Local dev**: Auth still uses the session cookie; when Supabase env vars are missing the login flow is disabled and only anonymous pages render (tests override auth via dependency overrides).

---

## Core Features

- Manually enter stock positions (ticker, company name, cost basis, initial purchase date, investment type: **long-term** or **short-term**)
- Automatic market data refresh via Alpha Vantage or Twelve Data
- Per-user configurable rule sets — enable/disable rules and edit their parameters on the Rules page
- Display a clear **Sell / Trim / Hold** verdict per position with the specific rule(s) that triggered it
- Prioritize positions by urgency (Sell → Trim → Hold)
- Summary dashboard across all positions
- Per-position key levels (support/resistance) that feed into the failed-breakout rule
- Per-position sector benchmark ticker for the relative-weakness rule
- SQLite for local dev; Postgres for production (same ORM + Alembic migrations)
- User tiers: **free** (5 tickers, 5 refreshes/day) and **full_access** (unlimited)

---

## The Rules

Rules are user-configurable per investment type. The defaults are:

### Default Long-Term Rules
| Key | Verdict | Condition |
|---|---|---|
| `SELL_MA_ALL` | Sell | Weekly close below the 20-week SMA |
| `TRIM-10PCT` | Trim | Price > 10% above cost basis |
| `HOLD-ABOVE-COST` | Hold | Price at or above cost basis |

### Default Short-Term Rules
| Key | Verdict | Condition |
|---|---|---|
| `SELL_MA_ALL` | Sell | Daily close below the 21-day SMA |
| `TRIM-10PCT` | Trim | Price > 10% above cost basis |
| `HOLD-ABOVE-COST` | Hold | Price at or above cost basis |

### Optional Rules (available in the Rules catalog for both investment types)
| Key | Verdict | Condition |
|---|---|---|
| `SELL_EXTENSION_ATR` | Sell | Price ≥ 10× ATR-14 above the daily SMA-50 |
| `TRIM_EXTENSION_ATR` | Trim | Price ≥ 8× ATR-14 above the daily SMA-50 |
| `TRIM_WEEKLY_UPPER_WICK` | Trim | Latest weekly candle shows long upper wick near recent highs |
| `SELL_WEEKLY_DISTRIBUTION_CLUSTER` | Sell | 3+ high-volume red weeks in last 8 weeks |
| `TRIM_WEEKLY_DISTRIBUTION_CLUSTER` | Trim | 2+ high-volume red weeks in last 8 weeks |
| `SELL_WEEKLY_LOWER_HIGH_LOWER_LOW` | Sell | Confirmed lower weekly high followed by lower weekly low |
| `TRIM_WEEKLY_FIRST_LOWER_HIGH` | Trim | First confirmed weekly lower high after a prior uptrend |
| `TRIM_RELATIVE_WEAKNESS_VS_SECTOR` | Trim | Position underperforms its sector benchmark by ≥10% over 63 days while benchmark is up ≥8% |
| `SELL_FAILED_BREAKOUT_RECLAIM` | Sell | Price breaks above a key level, fades back, and fails to reclaim it |

Rules are evaluated highest-priority first; the first triggered rule wins. Sell > Trim > Hold.

---

## Data Model

### Position (stored in DB)
| Field | Type | Notes |
|---|---|---|
| `id` | integer (PK) | Auto-generated |
| `ticker` | string | e.g. "AAPL" |
| `company_name` | string | e.g. "Apple Inc." |
| `cost_basis` | float | Per share, USD |
| `initial_purchase_date` | date | |
| `investment_type` | string | `"long-term"` or `"short-term"` |
| `current_price` | float | Manually entered fallback |
| `notes` | string (optional) | Freeform |
|| `daily_close` | float (optional) | Cached from market data provider |
|| `daily_sma_21` | float (optional) | Cached from market data provider |
|| `daily_market_date` | date (optional) | Date of cached daily data |
|| `daily_retrieved_at` | datetime (optional) | When daily data was fetched |
|| `weekly_close` | float (optional) | Cached from market data provider |
|| `weekly_sma_20` | float (optional) | Cached from market data provider |
|| `weekly_market_date` | date (optional) | Date of cached weekly data |
|| `weekly_retrieved_at` | datetime (optional) | When weekly data was fetched |
|| `refresh_error` | string (optional) | Last refresh error message |
|| `refresh_in_progress` | boolean | True while a refresh is running |
|| `refresh_started_at` | datetime (optional) | When the current refresh began |
|| `previous_verdict` | string (optional) | Verdict from last refresh cycle |
|| `sector_benchmark_ticker` | string (optional) | For relative-weakness rule |
|| `user_id` | string (FK → users) | Owner |

### Other ORM models
- **User** — Supabase Auth UUID, email, display_name, tier, is_admin, refresh quota
- **PositionKeyLevel** — manually flagged support/resistance levels per position
- **MarketIndicatorCache** — cached SMA values keyed by (ticker, interval, time_period)
- **MarketAtrCache** — cached ATR values keyed by (ticker, interval, time_period)
- **MarketWeeklyBarCache** — cached weekly OHLCV bars per ticker
- **MarketDailyBarCache** — cached daily OHLCV bars per ticker
- **StrategyRuleConfig** — per-user, per-investment-type rule enable/disable + params

### Derived / Computed Fields (not stored, calculated at runtime)
- `percent_gain` = (current_price - cost_basis) / cost_basis × 100
- `hold_duration` = today - initial_purchase_date
- `verdict` = Sell | Trim | Hold (output of rule engine)
- `triggered_rules` = list of rule labels that fired

---

## Rule Engine Design
- Rules are pure Python functions in `rule_engine.py`: `(PositionLike, MarketSignals, params) → Optional[RuleResult]`
- Each rule is registered in `RULE_CATALOG` as a `RuleSpec` with a key, name, description, verdict, and supported investment types
- User-selected rules are stored in `StrategyRuleConfig` (DB) and loaded via `rule_config.py`
- `_enrich_position()` in `main.py` assembles `MarketSignals` from the indicator/ATR/bar caches and calls `get_verdict()`
- Do NOT put override logic, route logic, or DB queries inside `rule_engine.py` — it must stay pure and testable in isolation

---

## Tech Stack
- **Backend**: Python 3.13, FastAPI
- **Database**: SQLite (local dev) or Postgres (production) via SQLAlchemy 2 ORM + Alembic migrations
- **Frontend**: Jinja2 templates with plain HTML and CSS served directly by FastAPI
- **Auth**: Supabase Auth — PKCE flow for Google OAuth and email OTP; JWKS-based JWT verification in `auth.py` (login flow is disabled when required Supabase env vars are missing)
- **Market data**: Alpha Vantage or Twelve Data via provider abstraction in `app/market_data/`; auto-detected from which API key is set
- **Styling**: Plain CSS — clean, minimal, functional aesthetic

### Project Structure
```
app/
├── main.py              # FastAPI app, all routes, _enrich_position
├── models.py            # SQLAlchemy ORM models
├── schemas.py           # Pydantic request/response schemas
├── database.py          # Engine, session factory, UnitOfWork DI helpers
├── unit_of_work.py      # UnitOfWork pattern (user-scoped DB session)
├── repositories.py      # Per-user data access layer
├── rule_engine.py       # All sell/trim/hold rule logic — pure functions only
├── rule_config.py       # Persistence helpers for StrategyRuleConfig
├── auth.py              # Supabase PKCE + JWKS JWT verification
├── config.py            # Env-var-backed configuration (pydantic-settings)
├── csrf.py              # CSRF token generation and validation
├── tiers.py             # Tier limits and enforcement helpers
├── notifications.py     # Flash-message helpers
├── market_data/
│   ├── provider.py      # Provider abstraction (Alpha Vantage / Twelve Data)
│   ├── service.py       # Refresh orchestration: fetch → cache → enrich
│   ├── cache_repos.py   # DB access for market indicator/ATR/bar caches
│   ├── staleness.py     # Cache staleness and stale-cleanup logic
│   └── exceptions.py    # Market data error types
├── alpha_vantage_client.py  # Alpha Vantage HTTP client
├── twelve_data_client.py    # Twelve Data HTTP client
├── templates/           # Jinja2 HTML templates
│   ├── base.html
│   ├── splash.html
│   ├── login.html
│   ├── portfolio.html
│   ├── add_position.html
│   ├── edit_position.html
│   ├── rules.html
│   ├── admin.html
│   └── privacy.html
└── static/
    └── styles.css
alembic/                 # Database migrations
tests/                   # pytest suite
```

---

## Key User Flows

### 1. Add a position
User fills in ticker, company name, cost basis, initial purchase date, current price, investment type → FastAPI saves to SQLite/Postgres → redirects to portfolio view.

### 2. Evaluate portfolio
On portfolio page load: positions fetched → market data assembled from indicator/ATR/bar caches → each position passed through `_enrich_position()` → rule engine evaluated → verdicts rendered with color coding (red = Sell, yellow = Trim, green = Hold).

### 3. Refresh market data
User clicks Refresh on a position or Refresh All → `POST /refresh/{id}` (or `/refresh-all`) → background fetch from provider → caches updated → page reloads with fresh verdicts.

### 4. Configure rules
User navigates to Rules page → enables/disables rules per investment type, edits parameters → saved to `StrategyRuleConfig` → applied on next portfolio evaluation.

### 5. Update a position
User clicks Edit → updates fields → FastAPI updates the record → portfolio reloads with recalculated verdicts.

---

## UI / Vibe
- Clean, data-focused interface — think a lightweight financial dashboard
- Minimal color palette: use red/yellow/green only for verdict status indicators
- No decorative graphics or illustrations
- Readable at a glance — the verdict and reasoning should be immediately visible without clicking
- Works well on desktop browser (mobile responsiveness is a nice-to-have)

---

## Testing
- Use `pytest` for all tests; `pytest-mock` for mocking dependencies
- Run: `python -m pytest` from the repo root
- Tests use an in-memory SQLite database via a `_setup_db` autouse fixture that overrides the FastAPI DI dependencies (`get_uow`, `get_authenticated_uow`, `get_optional_uow`)
- The fixture also includes a `before_flush` SQLAlchemy event listener that assigns `id = "test-user-id"` to any new `User` and `user_id = "test-user-id"` to any new `Position` or `StrategyRuleConfig` object — copy this pattern from `test_refresh_route.py` when writing new integration tests
- Rule engine unit tests must not touch the database or any HTTP client; pass a `Position` object and a `MarketSignals` struct directly
- CSRF tokens: use the `csrf_form_data(client)` helper from `tests/csrf_utils.py` when posting forms in tests

---

## Coding Conventions
- Keep `rule_engine.py` pure — no DB access, no HTTP calls, no FastAPI imports
- Keep post-rule enrichment logic in `_enrich_position()` in `main.py`, not in the rule engine
- Use Pydantic models (`schemas.py`) for all request/response validation
- Prefer SQLAlchemy ORM/models for database interaction; use SQLAlchemy Core/`text()` only when necessary (e.g., SQLite schema patching or PostgreSQL `set_config` for RLS) and always parameterize inputs
- Use snake_case for all Python variables, functions, and file names
- Comment each rule in `rule_engine.py` with a plain-English description of what it checks
- Validate inputs at the Pydantic schema level (no empty tickers, no negative prices, no future purchase dates)
- All POST/DELETE routes must validate CSRF via `Depends(validate_csrf)`
- Prefer simple, readable code over clever optimizations
- `tier` and `is_admin` on the `User` model are security-sensitive — only write them from `admin_*` route handlers; never from user-submitted request data