# StockSentinal

A personal-then-multi-user web app that helps you decide when to **sell**, **trim**, or **hold** stock positions. You enter your positions (ticker, cost basis, purchase date, long- vs short-term horizon), the app pulls market data from your chosen provider, and a rule engine produces a clear verdict for each holding along with the rules that triggered it.

Live: [www.stocksentinal.com](https://www.stocksentinal.com)

## Features

- **Sell / Trim / Hold verdicts** per position with the specific rule(s) that fired
- **Long-term and short-term rule sets** evaluated separately (e.g. weekly 20-MA breaks for long-term, daily 21-MA breaks for short-term)
- **Configurable rules** — enable/disable rules and edit their parameters per investment type on the Rules page
- **Mark as Trimmed** — after trimming a position, click "Mark as Trimmed" to acknowledge it; the verdict overrides to Hold until you clear the flag or a Sell rule fires
- **Custom key levels** per position (support/resistance) that feed into the rule engine
- **Market data** pulled from Alpha Vantage or Twelve Data, with batching support on Twelve Data to stay inside free-tier limits
- **Multi-user** support via Supabase Auth (Google OAuth out of the box); falls back to a single-user mode for local dev
- **Public splash page** for anonymous visitors; portfolio view is authenticated
- **SQLite for local dev, Postgres for production** (via the same SQLAlchemy models and Alembic migrations)

## Tech stack

FastAPI · SQLAlchemy 2 · Alembic · Jinja2 templates · Pydantic v2 · Supabase Auth (PKCE + JWKS) · pytest

## Quickstart (local)

Requires Python 3.13.

```bash
# Clone and install in editable mode with dev deps
git clone https://github.com/ryankmcintyre/StockSentinal.git
cd StockSentinal
pip install -e . pytest pytest-mock httpx

# Apply migrations (creates ./stocksentinal.db by default)
alembic upgrade head

# Run the app
uvicorn app.main:app --reload
```

Open <http://localhost:8000>. With no `SUPABASE_URL` configured the app runs in single-user mode and skips the login flow — perfect for local development.

### Run the tests

```bash
python -m pytest
```

## Configuration

All configuration is read from environment variables (a local `.env` file is auto-loaded). Nothing here is required for a minimal local run — the app will start with sensible defaults and warn loudly about anything insecure.

### Core

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./stocksentinal.db` | Any SQLAlchemy URL. Use `postgresql+psycopg2://...` for Postgres. |
| `PORT` | `8000` | Uvicorn listen port (used by the Docker image). |
| `LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. |

### Market data provider

The provider is auto-detected from whichever API key you set. Set the key, leave `MARKET_DATA_PROVIDER` unset, and the app does the right thing.

| Variable | Default | Notes |
|---|---|---|
| `ALPHA_VANTAGE_API_KEY` | — | Enables Alpha Vantage. |
| `TWELVE_DATA_API_KEY` | — | Enables Twelve Data. Takes precedence if both keys are set. |
| `MARKET_DATA_PROVIDER` | auto | Explicit override: `alphavantage` or `twelvedata`. |
| `ALPHA_VANTAGE_MIN_INTERVAL_SECONDS` | `12.0` | Gate between Alpha Vantage calls (free tier: 5/min). |
| `TWELVE_DATA_MIN_INTERVAL_SECONDS` | `8.0` | Gate between Twelve Data calls (free tier: 8/min, 800/day). |
| `TWELVE_DATA_CREDITS_PER_MINUTE` | — | Optional rolling 60-second credit-budget gate for Twelve Data. **Takes precedence over `TWELVE_DATA_MIN_INTERVAL_SECONDS`.** When set, up to this many calls per minute may run concurrently; when unset, the app falls back to strict per-call spacing. See below. |

Paid tiers can lower these intervals without changing code. Recommended starting points:

| Provider / tier | Calls/min | Suggested `*_MIN_INTERVAL_SECONDS` |
|---|---:|---:|
| Twelve Data Free | 8 | `8.0` |
| Twelve Data Grow | 55 | `1.1` (or set `TWELVE_DATA_CREDITS_PER_MINUTE=50`) |
| Alpha Vantage Free | 5 | `12.0` |
| Alpha Vantage Premium | 75 | `0.8` |

#### `TWELVE_DATA_CREDITS_PER_MINUTE` (Twelve Data concurrency)

A single position refresh fetches several independent Twelve Data series (e.g. daily + weekly, plus a benchmark for relative-weakness rules). How those calls are paced depends on this variable:

- **Set** (e.g. `TWELVE_DATA_CREDITS_PER_MINUTE=50`): Twelve Data uses a rolling 60-second credit-budget gate. As long as fewer than the budgeted number of calls happened in the trailing minute, calls proceed **without delay**, so the independent daily/weekly/benchmark fetches in one refresh run **concurrently**. A single default refresh completes in ~1–2s.
- **Unset**: the app falls back to the strict per-call `TWELVE_DATA_MIN_INTERVAL_SECONDS` gate, which spaces **every** call out in series. With the default `8.0`s interval, two serialized calls plus HTTP make a single refresh take ~10s.

**This variable takes precedence over `TWELVE_DATA_MIN_INTERVAL_SECONDS`** for Twelve Data. Pick a value safely **under your plan's hard cap** to leave headroom for retries and other traffic — e.g. `50` for the Grow plan (55/min cap, 5-credit headroom). Even when this is unset, concurrent prewarm fetches still overlap their interval waits (the gate reserves each call's slot without holding a lock during the wait), so pacing is respected on average without fully serializing every call.

On startup, the app logs the active provider and effective rate-limit interval so you can confirm what is configured.

Without a key, the app starts but every refresh fails with a clear error. See [issue #70](https://github.com/ryankmcintyre/StockSentinal/issues/70) for ongoing refresh-performance work.

### Authentication (Supabase)

Set these to enable multi-user mode. Leave `SUPABASE_URL` empty for single-user local dev.

| Variable | Required when | Notes |
|---|---|---|
| `SUPABASE_URL` | enabling auth | Your project URL, e.g. `https://abcd.supabase.co`. |
| `SUPABASE_PUBLISHABLE_KEY` | `SUPABASE_URL` set | Used for Supabase auth API calls (OTP and OAuth). |
| `SESSION_SECRET_KEY` | `SUPABASE_URL` set | Long random string; signs the app's session cookie. The app **refuses to start** with auth enabled if this is missing. |
| `SUPABASE_AUTH_PROVIDERS` | optional | Comma-separated list of social providers. Defaults to `google`. |

#### Supabase setup (one-time)

1. Create a Supabase project; copy the project URL and the **publishable** anon key.
2. **Authentication → Providers → Google** — enable Google and paste your Google OAuth client ID + secret.
3. **Authentication → Providers → Email** — ensure Email auth is enabled (for OTP code sign-in).
4. **Authentication → URL Configuration**:
   - **Site URL**: your production URL (e.g. `https://www.stocksentinal.com`).
   - **Redirect URLs**: add every host the app runs on, including `http://localhost:8000/auth/callback` for local dev, and any custom domain equivalents.
5. **Google Cloud Console → Credentials → OAuth client**: add `https://<your-project>.supabase.co/auth/v1/callback` as an authorized redirect URI. (Google redirects to Supabase, not to your app, so your custom domain doesn't need to be listed here.)

#### Email OTP template

In Supabase Dashboard → **Authentication → Email Templates → Magic Link**, replace the default template with the following to send a 6-digit code instead of a clickable link:

**Subject:** `Your StockSentinal sign-in code`

**Body:**

```html
<h2>StockSentinal sign-in code</h2>
<p>Use this code to sign in or create your account:</p>
<p style="font-size: 28px; font-weight: bold; letter-spacing: 4px;">
  {{ .Token }}
</p>
<p>This code expires automatically. If you didn't request it, you can ignore this email.</p>
```

#### OTP expiration

In Supabase Dashboard → **Authentication → Providers → Email → Email OTP Expiration**, set the OTP lifetime. The default is 60 seconds; a value of 300–600 seconds (5–10 minutes) is recommended for this app.

### Performance & observability

These variables tune the Postgres connection pool, RLS GUC latching, and refresh-time diagnostics. None are required for SQLite / local dev.

| Variable | Default | Notes |
|---|---|---|
| `PG_POOL_MODE` | unset | Set to `session` to enable the GUC latch (see below). **Must be paired with Supavisor port `:5432`.** |
| `REFRESH_PROFILING_ENABLED` | `0` | Set to `1` to emit `[profile refresh.single …]` log lines. |

#### `PG_POOL_MODE` and Supavisor port choice ⚠️

**This is the #1 performance win and the most dangerous footgun in this config.**

Supabase exposes two Supavisor pooler ports:

| Port | Mode | Behavior |
|---|---|---|
| `:5432` | **Session** | One client connection = one persistent Postgres backend for its lifetime. A session-scoped GUC written at the start of connection stays present for all subsequent transactions. |
| `:6543` | **Transaction** | The same DBAPI connection can be routed to a *different* physical Postgres backend between transactions. A GUC written in transaction N may be gone by transaction N+1. |

The app writes `app.current_user_id` via `set_config('app.current_user_id', …, false)` at the start of each connection to enforce row-level security. In transaction mode this must be re-sent on every transaction (safe but slower). In session mode it only needs to be sent once per physical connection — subsequent transactions on the same connection skip the `SELECT set_config(…)` call entirely.

**Setting `PG_POOL_MODE=session` enables the latch that skips redundant `set_config` calls. It is only safe when `DATABASE_URL` uses port `:5432` (session-mode Supavisor).** If you set `PG_POOL_MODE=session` but keep `:6543` in `DATABASE_URL`, the latch will incorrectly skip sending the GUC on connections that have been recycled to a new backend, which silently leaks one user's RLS context to another user's queries.

**Connection string examples:**

```
# Session mode (use with PG_POOL_MODE=session) — recommended:
postgresql+psycopg2://postgres.PROJECT_ID:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres

# Transaction mode (PG_POOL_MODE unset or any value other than "session"):
postgresql+psycopg2://postgres.PROJECT_ID:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres
```

#### QueuePool sizing

The SQLAlchemy `QueuePool` is configured with `pool_size=5`, `max_overflow=5`, and `pool_recycle=1800`. A single Render instance therefore holds at most 10 Postgres sessions open at any time. Scale by instance count: two instances = up to 20 sessions, etc. Supabase free-tier Supavisor caps concurrent sessions, so keep this in mind when scaling horizontally.

#### `REFRESH_PROFILING_ENABLED`

Set to `1` (or `true`/`yes`/`on`) to enable detailed per-phase timing on each single-position refresh. When enabled, every completed `refresh_position` call emits structured per-phase timing at `INFO` level.

Each `key=Nms` block is a phase:

| Phase | What it measures |
|---|---|
| `rule_config` | Ensure rule defaults seeded + fetch enabled rules + compute requirements |
| `prewarm` | Concurrent pre-warm API calls (daily/weekly/benchmark) |
| `daily_refresh` | Fetch and cache daily close/SMA snapshot |
| `weekly_refresh` | Fetch and cache weekly close/SMA snapshot (long-term only) |
| `indicator_cache` | Refresh SMA indicator caches for configured rules |
| `atr_cache` | Refresh ATR indicator caches for configured rules |
| `weekly_bar_cache` | Refresh weekly OHLCV bar cache |
| `daily_bar_cache` | Refresh daily OHLCV bar cache |
| `verdicts` | Run rule engine and compute/store `computed_verdict` |
| `commit` | Final DB commit |

Defaults to disabled in production — the extra timers and SQL-event listeners have a measurable overhead.

#### Frontend debug flags

To surface poll-timing logs in the browser console without modifying code:

- **One-off**: append `?refresh_debug=1` to the portfolio URL.
- **Persistent**: run `localStorage.setItem('refreshDebug', '1')` in the browser console. Clear with `localStorage.removeItem('refreshDebug')`.

When active, the JS polling loop logs how long each poll cycle takes and when the UI transitions between "refreshing" and "idle" states.

## Docker

A production-oriented `Dockerfile` and a `docker-compose.yml` are included.

### Build and run

```bash
docker build -t stocksentinal .

docker run --rm \
  -p 8000:8000 \
  -v stocksentinal-data:/data \
  -e ALPHA_VANTAGE_API_KEY=your-key \
  stocksentinal
```

The container defaults `DATABASE_URL` to `sqlite:////data/stocksentinal.db` so a mounted volume persists across runs. Override `DATABASE_URL` to point at Postgres instead.

### Docker Compose (live reload)

```bash
docker compose up --build
```

Compose reads from your shell environment or a local `.env` file automatically.

## Deployment

The repo ships with a `render.yaml` for one-click deploys to [Render](https://render.com):

- Service runs `uvicorn app.main:app` on the Render-assigned `$PORT`.
- Build command runs `alembic upgrade head`, so migrations apply on every deploy.
- Set `DATABASE_URL` in the Render dashboard to your Supabase pooled connection string — **use port `:5432` (session mode) when `PG_POOL_MODE=session`** (see [Performance & observability](#performance--observability) below for the port choice explanation).
- Set `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SESSION_SECRET_KEY`, and a market data API key in the dashboard as well.

Custom domains work out of the box — the app uses `request.base_url` for OAuth callbacks, so no extra config is needed once the domain is added in both Render and Supabase's redirect URL list.

## Project layout

```
app/
├── main.py            # FastAPI app and routes
├── auth.py            # Supabase PKCE + JWKS verification
├── database.py        # SQLAlchemy engine, sessions, UnitOfWork DI
├── models.py          # ORM models
├── schemas.py         # Pydantic request/response schemas
├── repositories.py    # Per-user-scoped data access
├── rule_engine.py     # Sell/Trim/Hold rules (isolated from UI)
├── rule_config.py     # Persistence helpers for StrategyRuleConfig
├── config.py          # Env-var-backed configuration
├── market_data/
│   ├── provider.py    # Provider abstraction (Alpha Vantage / Twelve Data)
│   ├── service.py     # Refresh orchestration: fetch → cache → enrich
│   ├── cache_repos.py # DB access for market indicator/ATR/bar caches
│   ├── staleness.py   # Cache staleness and stale-cleanup logic
│   └── exceptions.py  # Market data error types
├── templates/         # Jinja2 templates (splash, portfolio, edit, etc.)
└── static/            # CSS and a small amount of JS
alembic/               # Database migrations
tests/                 # pytest suite
```

## Contributing

Issues and PRs are welcome. The rule engine is intentionally isolated in `app/rule_engine.py` so new rules can be added without touching routes or templates. Add tests in `tests/test_rule_engine.py` for any new rule and run `python -m pytest` before pushing.
