import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from math import isclose
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv
import httpx

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

load_dotenv()

from app.auth import (
    PKCE_COOKIE_NAME,
    PKCE_MAX_AGE_SECONDS,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    RequiresLoginException,
    decode_pkce_cookie,
    encode_pkce_cookie,
    encode_session_cookie,
    generate_pkce_pair,
    get_current_user_id,
    is_https,
    verify_supabase_jwt,
)
from app.config import (
    get_log_level,
    get_market_data_api_key,
    get_market_data_min_interval_seconds,
    get_market_data_provider,
    get_market_data_provider_display_name,
    get_supabase_auth_providers,
    get_supabase_publishable_key,
    get_supabase_url,
    has_supabase_publishable_key,
    has_session_secret_key,
)
from app.notifications import send_new_member_notification
from app.csrf import CSRFMiddleware, csrf_token_for_template, validate_csrf
from app.database import (
    SessionLocal,
    engine,
    get_authenticated_uow,
    get_admin_uow,
    get_optional_uow,
    get_uow,
    init_db,
)
from app.market_data.exceptions import MarketDataError, MarketDataSymbolNotFound
from app.market_data.provider import AlphaVantageProvider, TwelveDataProvider
from app.market_data.service import MarketDataService
from app.models import Position, PositionKeyLevel, User
from app.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork
from app.rule_config import (
    add_ma_condition,
    get_enabled_rule_selections_by_investment_type,
    get_rule_management_sections,
    remove_ma_condition,
    update_strategy_rule_config,
)
from app.rule_engine import (
    DailyClosePoint,
    MarketSignals,
    StrategyRuleSelection,
    WeeklyOhlcBar,
    compute_hold_duration_days,
    compute_percent_gain,
    evaluate_position,
    get_verdict,
)
from app.schemas import InvestmentType, Verdict
from app.tiers import TIER_LIMITS, TierLimitExceeded, check_and_consume_refresh, check_can_add_position

log_level = get_log_level()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger().setLevel(log_level)

# Suppress third-party HTTP loggers that leak sensitive query parameters
# (e.g. urllib3 logs full request URLs including market-data API keys)
_HTTP_LOGGERS = ("urllib3", "requests", "httpcore", "httpx", "http.client")
for _name in _HTTP_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
PRICE_COMPARISON_ABS_TOLERANCE = 0.005


def _create_market_data_provider():
    if get_market_data_provider() == "twelvedata":
        return TwelveDataProvider()
    return AlphaVantageProvider()


_provider = _create_market_data_provider()
_market_service = MarketDataService(_provider)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Stock Sentinel — initializing database")
    init_db()
    if get_market_data_api_key():
        logger.info(
            "Market data provider: %s (rate-limit interval %gs between API calls)",
            get_market_data_provider_display_name(),
            get_market_data_min_interval_seconds(),
        )
    else:
        logger.info("Market data provider: unconfigured")
    cleared = _clear_all_stale_refresh_flags()
    if cleared:
        logger.info("Cleared %d stale refresh-in-progress flags", cleared)
    logger.info("Database initialized, application ready")
    yield


app = FastAPI(title="Stock Sentinel", lifespan=lifespan)
app.add_middleware(CSRFMiddleware)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["csrf_token"] = csrf_token_for_template


@app.exception_handler(RequiresLoginException)
async def requires_login_handler(request: Request, _exc: RequiresLoginException):
    return RedirectResponse(url="/auth/login", status_code=303)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VERDICT_PRIORITY = {Verdict.sell: 0, Verdict.trim: 1, Verdict.hold: 2}

# Human-readable display names for Supabase Auth social providers.
# Falls back to title-cased provider id for any unlisted provider.
_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "google": "Google",
    "github": "GitHub",
    "discord": "Discord",
    "apple": "Apple",
    "twitter": "Twitter",
    "facebook": "Facebook",
    "azure": "Azure AD",
    "gitlab": "GitLab",
    "bitbucket": "Bitbucket",
    "spotify": "Spotify",
    "slack": "Slack",
    "twitch": "Twitch",
    "linkedin": "LinkedIn",
    "notion": "Notion",
}
REFRESH_STALE_TIMEOUT_MINUTES = 5
# Client poll ceiling: keep polling past the backend stale timeout (plus a
# margin) so the UI never gives up while a refresh is genuinely still running.
REFRESH_POLL_TIMEOUT_MS = (REFRESH_STALE_TIMEOUT_MINUTES + 2) * 60 * 1000
FLASH_MESSAGES = {
    "refresh_limit": "You've used 5 of 5 refreshes today. Your limit resets at midnight UTC.",
    "admin_updated": "Admin user settings updated.",
}


def _get_request_user_id(request: Request, uow: UnitOfWork | None = None) -> str | None:
    """Return the authenticated user id, falling back to a scoped test UoW when needed."""
    user_id = get_current_user_id(request)
    if user_id is not None:
        return user_id
    if uow is None:
        return None
    for repo_name in ("positions", "rule_configs"):
        repo = getattr(uow, repo_name, None)
        scoped_user_id = getattr(repo, "_user_id", None)
        if scoped_user_id:
            return scoped_user_id
    return None


def _get_current_user(request: Request, uow: UnitOfWork) -> User | None:
    user_id = _get_request_user_id(request, uow)
    if not user_id:
        return None
    return uow.users.get_by_id(user_id)


def _flash_message(request: Request) -> str | None:
    code = request.query_params.get("flash")
    return FLASH_MESSAGES.get(code) if code else None


def _redirect_with_refresh_limit_flash() -> RedirectResponse:
    return RedirectResponse(url="/?flash=refresh_limit", status_code=303)


def _admin_redirect_with_flash() -> RedirectResponse:
    return RedirectResponse(url="/admin?flash=admin_updated", status_code=303)


def _url_safe_edit_position_path(position_id: int) -> str:
    position_id_segment = str(int(position_id))
    return f"/edit/{position_id_segment}"


def _supabase_auth_configured() -> bool:
    """Return True when auth routes have the required server-side config."""
    return (
        get_supabase_url() is not None
        and has_session_secret_key()
        and has_supabase_publishable_key()
    )


def _daily_bars_for_position(pos: Position, all_daily_bars: dict[str, list]) -> dict[str, list] | None:
    """Build the per-position {ticker: bars} dict for the relative-weakness rule."""
    relevant: dict[str, list] = {}
    pos_bars = all_daily_bars.get(pos.ticker)
    if pos_bars:
        relevant[pos.ticker.upper()] = pos_bars
    benchmark = getattr(pos, "sector_benchmark_ticker", None)
    if benchmark:
        bench_bars = all_daily_bars.get(benchmark.upper())
        if bench_bars:
            relevant[benchmark.upper()] = bench_bars
    return relevant or None


def _mark_positions_refresh_state(
    uow: UnitOfWork,
    position_ids: list[int],
    *,
    in_progress: bool,
) -> int:
    """Set refresh status fields for one or more positions and commit."""
    if not position_ids:
        return 0

    positions = uow.positions.get_by_ids(position_ids)
    now = datetime.now()
    for pos in positions:
        pos.refresh_in_progress = in_progress
        pos.refresh_started_at = now if in_progress else None
        if in_progress:
            pos.refresh_error = None
    uow.commit()
    return len(positions)


def _clear_stale_refresh_flags(uow: UnitOfWork) -> int:
    """Reset stale in-progress flags left behind by interrupted refreshes."""
    cutoff = datetime.now() - timedelta(minutes=REFRESH_STALE_TIMEOUT_MINUTES)
    cleared_count = uow.positions.clear_stale_refreshing(cutoff)
    if not cleared_count:
        return 0

    uow.commit()
    return cleared_count


def _clear_all_stale_refresh_flags() -> int:
    """Reset stale refresh flags across all users during application startup."""
    cutoff = datetime.now() - timedelta(minutes=REFRESH_STALE_TIMEOUT_MINUTES)
    with engine.begin() as conn:
        result = conn.execute(
            text(
                """
                UPDATE positions
                SET refresh_in_progress = :not_in_progress,
                    refresh_started_at = NULL
                WHERE refresh_in_progress = :in_progress
                  AND refresh_started_at IS NOT NULL
                  AND refresh_started_at < :cutoff
                """
            ),
            {
                "not_in_progress": False,
                "in_progress": True,
                "cutoff": cutoff,
            },
        )
        return result.rowcount if result.rowcount is not None else 0


def _clear_position_market_data(position: Position) -> None:
    """Clear cached market-data fields from a position in memory.

    Caller must commit the session for the change to persist.
    """
    position.daily_close = None
    position.daily_sma_21 = None
    position.daily_market_date = None
    position.daily_retrieved_at = None
    position.weekly_close = None
    position.weekly_sma_20 = None
    position.weekly_market_date = None
    position.weekly_retrieved_at = None
    position.refresh_error = None


def _enrich_position(
    pos: Position,
    enabled_rules_by_type: dict[str, list[StrategyRuleSelection]] | None = None,
    indicator_cache: dict[tuple[str, int], tuple[float | None, float | None]] | None = None,
    atr_cache: dict[tuple[str, int], float | None] | None = None,
    weekly_bars: list | None = None,
    daily_bars_by_ticker: dict[str, list] | None = None,
) -> dict:
    """Run rule engine on a Position and return a dict with all display fields."""
    # Build market signals from cached market data
    signals = MarketSignals(
        daily_close=pos.daily_close,
        daily_sma_21=pos.daily_sma_21,
        weekly_close=pos.weekly_close,
        weekly_sma_20=pos.weekly_sma_20,
    )

    # Populate flexible ma_signals from indicator cache
    if indicator_cache:
        signals.ma_signals = dict(indicator_cache)

    # Populate atr_signals from ATR cache
    if atr_cache:
        signals.atr_signals = dict(atr_cache)

    # Populate weekly OHLC history from the weekly bar cache
    if weekly_bars:
        signals.weekly_ohlc_history = [
            WeeklyOhlcBar(
                bar_date=bar.bar_date,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in weekly_bars
        ]

    # Populate daily close history per ticker (issue #22 — relative weakness)
    if daily_bars_by_ticker:
        signals.daily_close_history = {
            ticker.upper(): [
                DailyClosePoint(bar_date=bar.bar_date, close=bar.close)
                for bar in bars
            ]
            for ticker, bars in daily_bars_by_ticker.items()
        }

    # Use cached daily close as effective price when available, otherwise manual
    effective_price = pos.daily_close if pos.daily_close is not None else pos.current_price

    class _PositionPriceProxy:
        """Delegate to a Position but override current_price for rule evaluation."""

        def __init__(self, original_pos: Position, current_price: float):
            self._original_pos = original_pos
            self.current_price = current_price

        def __getattr__(self, name):
            return getattr(self._original_pos, name)

    eval_pos = _PositionPriceProxy(pos, effective_price)

    configured_rules = None
    if enabled_rules_by_type is not None:
        configured_rules = enabled_rules_by_type.get(pos.investment_type)

    triggered = evaluate_position(eval_pos, signals=signals, configured_rules=configured_rules)
    verdict = get_verdict(triggered)
    reason_sort_value = (
        pos.refresh_error
        or ("Refreshing..." if pos.refresh_in_progress else "")
        or (triggered[0].description if triggered else "")
    )
    return {
        "id": pos.id,
        "ticker": pos.ticker,
        "company_name": pos.company_name,
        "cost_basis": pos.cost_basis,
        "current_price": pos.current_price,
        "effective_price": effective_price,
        "investment_type": pos.investment_type,
        "initial_purchase_date": pos.initial_purchase_date,
        "notes": pos.notes,
        "sector_benchmark_ticker": pos.sector_benchmark_ticker,
        "percent_gain": compute_percent_gain(pos.cost_basis, effective_price),
        "hold_duration_days": compute_hold_duration_days(pos.initial_purchase_date),
        "verdict": verdict,
        "previous_verdict": pos.previous_verdict,
        "verdict_sort_priority": VERDICT_PRIORITY.get(verdict, 99),
        "triggered_rules": triggered,
        "reason_sort_value": reason_sort_value,
        # Market data status
        "daily_close": pos.daily_close,
        "daily_sma_21": pos.daily_sma_21,
        "daily_market_date": pos.daily_market_date,
        "daily_retrieved_at": pos.daily_retrieved_at,
        "weekly_close": pos.weekly_close,
        "weekly_sma_20": pos.weekly_sma_20,
        "weekly_market_date": pos.weekly_market_date,
        "weekly_retrieved_at": pos.weekly_retrieved_at,
        "refresh_error": pos.refresh_error,
        "refresh_in_progress": bool(pos.refresh_in_progress),
        "refresh_started_at": pos.refresh_started_at,
        "has_market_data": pos.daily_close is not None or pos.weekly_close is not None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
def health_check():
    """Lightweight liveness probe — no auth required."""
    return {"status": "ok", "app": "Stock Sentinel"}


@app.get("/privacy")
def privacy_page(request: Request):
    """Serve the privacy statement — no authentication required."""
    return templates.TemplateResponse(request, "privacy.html", {"current_user": None})


@app.get("/auth/login")
def login_page(request: Request):
    """Show the login page with sign-in options."""
    if get_current_user_id(request):
        return RedirectResponse(url="/", status_code=303)
    supabase_configured = _supabase_auth_configured()
    provider_ids = get_supabase_auth_providers()
    providers_with_labels = [
        {"id": p, "label": _PROVIDER_DISPLAY_NAMES.get(p, p.title())}
        for p in provider_ids
    ]
    email_sent = request.query_params.get("email_sent") == "1"
    otp_email = request.query_params.get("email", "")
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "supabase_configured": supabase_configured,
            "providers": providers_with_labels,
            "current_user": None,
            "email_sent": email_sent,
            "otp_email": otp_email,
        },
    )


@app.get("/auth/{provider}/authorize")
def oauth_authorize(provider: str, request: Request):
    """Initiate OAuth PKCE flow for the given provider."""
    supabase_url = get_supabase_url()
    providers = get_supabase_auth_providers()
    if not _supabase_auth_configured():
        return RedirectResponse(url="/auth/login?error=not_configured", status_code=303)
    if not supabase_url or provider not in providers:
        return RedirectResponse(url="/auth/login", status_code=303)

    code_verifier, code_challenge = generate_pkce_pair()
    pkce_cookie_value = encode_pkce_cookie(code_verifier)

    base_url = str(request.base_url).rstrip("/")
    redirect_to = f"{base_url}/auth/callback"
    auth_url = (
        f"{supabase_url}/auth/v1/authorize"
        f"?provider={provider}"
        f"&redirect_to={redirect_to}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )

    response = RedirectResponse(url=auth_url, status_code=303)
    response.set_cookie(
        key=PKCE_COOKIE_NAME,
        value=pkce_cookie_value,
        max_age=PKCE_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https(request),
    )
    return response


@app.post("/auth/email")
def email_auth_request(
    request: Request,
    email: str = Form(...),
    _csrf: None = Depends(validate_csrf),
):
    """Request an email OTP code from Supabase."""
    if not _supabase_auth_configured():
        return RedirectResponse(url="/auth/login?error=not_configured", status_code=303)

    supabase_url = get_supabase_url()
    supabase_publishable_key = get_supabase_publishable_key()

    try:
        resp = httpx.post(
            f"{supabase_url}/auth/v1/otp",
            headers={
                "apikey": supabase_publishable_key,
                "Content-Type": "application/json",
            },
            json={
                "email": email,
                "create_user": True,
            },
            timeout=10,
        )
        resp.raise_for_status()
    except Exception:
        logger.warning("Email OTP request failed", exc_info=True)
        return RedirectResponse(url="/auth/login?error=email_send_failed", status_code=303)

    return RedirectResponse(url=f"/auth/login?email_sent=1&email={quote(email, safe='')}", status_code=303)


@app.post("/auth/email/verify")
def email_auth_verify(
    request: Request,
    background_tasks: BackgroundTasks,
    email: str = Form(...),
    otp_code: str = Form(...),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_uow),
):
    """Verify a 6-digit email OTP code via Supabase and create an app session."""
    if not _supabase_auth_configured():
        return RedirectResponse(url="/auth/login?error=not_configured", status_code=303)

    supabase_url = get_supabase_url()
    supabase_publishable_key = get_supabase_publishable_key()

    try:
        resp = httpx.post(
            f"{supabase_url}/auth/v1/verify",
            headers={
                "apikey": supabase_publishable_key,
                "Content-Type": "application/json",
            },
            json={
                "email": email,
                "token": otp_code,
                "type": "email",
            },
            timeout=10,
        )
        resp.raise_for_status()
        token_data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Email OTP verification failed with status=%s body=%s",
            exc.response.status_code,
            exc.response.text,
        )
        return RedirectResponse(
            url=f"/auth/login?error=invalid_code&email={quote(email, safe='')}", status_code=303
        )
    except Exception:
        logger.warning("Email OTP verification failed", exc_info=True)
        return RedirectResponse(
            url=f"/auth/login?error=invalid_code&email={quote(email, safe='')}", status_code=303
        )

    access_token = token_data.get("access_token")
    if not access_token:
        logger.warning("Email OTP verify: no access_token in response")
        return RedirectResponse(
            url=f"/auth/login?error=no_token&email={quote(email, safe='')}", status_code=303
        )

    claims = verify_supabase_jwt(access_token)
    if not claims:
        return RedirectResponse(url="/auth/login?error=invalid_token", status_code=303)

    user_id = claims.get("sub")
    if not user_id:
        return RedirectResponse(url="/auth/login?error=no_subject", status_code=303)

    email_claim = claims.get("email")
    user_meta = claims.get("user_metadata") or {}
    display_name = (
        user_meta.get("full_name")
        or user_meta.get("name")
        or user_meta.get("preferred_username")
        or email_claim
    )

    user = uow.users.get_by_id(user_id)
    is_new_user = user is None
    if user is None:
        user = User(id=user_id, email=email_claim, display_name=display_name)
        uow.users.add(user)
    else:
        if email_claim and user.email != email_claim:
            user.email = email_claim
        if display_name and user.display_name != display_name:
            user.display_name = display_name
    uow.commit()

    if is_new_user:
        background_tasks.add_task(
            send_new_member_notification, email_claim, display_name
        )

    session_value = encode_session_cookie(user_id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_value,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https(request),
    )
    logger.info("User %s logged in via email OTP", user_id)
    return response


@app.get("/auth/callback")
def oauth_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    code: str | None = None,
    uow: UnitOfWork = Depends(get_uow),
):
    """Handle the OAuth callback from Supabase Auth."""
    if not code:
        logger.warning("OAuth callback received without code")
        return RedirectResponse(url="/auth/login?error=missing_code", status_code=303)

    if not _supabase_auth_configured():
        return RedirectResponse(url="/auth/login?error=not_configured", status_code=303)

    pkce_cookie = request.cookies.get(PKCE_COOKIE_NAME)
    code_verifier = decode_pkce_cookie(pkce_cookie) if pkce_cookie else None
    if not code_verifier:
        logger.warning("OAuth callback: missing or invalid PKCE cookie")
        return RedirectResponse(url="/auth/login?error=invalid_state", status_code=303)

    supabase_url = get_supabase_url()
    supabase_publishable_key = get_supabase_publishable_key()
    if not supabase_url:
        return RedirectResponse(url="/auth/login?error=not_configured", status_code=303)
    if not supabase_publishable_key:
        return RedirectResponse(url="/auth/login?error=not_configured", status_code=303)

    try:
        resp = httpx.post(
            f"{supabase_url}/auth/v1/token?grant_type=pkce",
            headers={"apikey": supabase_publishable_key},
            json={"auth_code": code, "code_verifier": code_verifier},
            timeout=10,
        )
        resp.raise_for_status()
        token_data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "OAuth token exchange failed with status=%s body=%s",
            exc.response.status_code,
            exc.response.text,
        )
        return RedirectResponse(url="/auth/login?error=token_exchange_failed", status_code=303)
    except Exception:
        logger.warning("OAuth token exchange failed", exc_info=True)
        return RedirectResponse(url="/auth/login?error=token_exchange_failed", status_code=303)

    access_token = token_data.get("access_token")
    if not access_token:
        logger.warning("OAuth callback: no access_token in response")
        return RedirectResponse(url="/auth/login?error=no_token", status_code=303)

    claims = verify_supabase_jwt(access_token)
    if not claims:
        return RedirectResponse(url="/auth/login?error=invalid_token", status_code=303)

    user_id = claims.get("sub")
    if not user_id:
        return RedirectResponse(url="/auth/login?error=no_subject", status_code=303)

    email = claims.get("email")
    user_meta = claims.get("user_metadata") or {}
    display_name = (
        user_meta.get("full_name")
        or user_meta.get("name")
        or user_meta.get("preferred_username")
        or email
    )

    user = uow.users.get_by_id(user_id)
    is_new_user = user is None
    if user is None:
        user = User(id=user_id, email=email, display_name=display_name)
        uow.users.add(user)
    else:
        if email and user.email != email:
            user.email = email
        if display_name and user.display_name != display_name:
            user.display_name = display_name
    uow.commit()

    if is_new_user:
        background_tasks.add_task(
            send_new_member_notification, email, display_name
        )

    session_value = encode_session_cookie(user_id)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_value,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_https(request),
    )
    response.delete_cookie(key=PKCE_COOKIE_NAME)
    logger.info("User %s logged in successfully", user_id)
    return response


@app.post("/auth/logout")
def logout(_csrf: None = Depends(validate_csrf)):
    """Clear the session cookie and redirect to login."""
    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie(key=SESSION_COOKIE_NAME)
    return response


@app.get("/")
def index(request: Request, uow: UnitOfWork | None = Depends(get_optional_uow)):
    """Root: splash page for anonymous visitors, portfolio dashboard for authenticated users."""
    if uow is None:
        return templates.TemplateResponse(request, "splash.html", {"current_user": None})
    return _portfolio_response(request, uow)


def _portfolio_response(request: Request, uow: UnitOfWork):
    """Build and return the portfolio dashboard template response."""
    user_id = _get_request_user_id(request, uow)
    current_user = _get_current_user(request, uow)
    _clear_stale_refresh_flags(uow)
    positions = uow.positions.list_all()
    enabled_rules_by_type = get_enabled_rule_selections_by_investment_type(uow, user_id=user_id)

    # Preload indicator cache for all tickers to avoid N+1 queries
    all_tickers = {p.ticker for p in positions}
    benchmark_tickers = {
        p.sector_benchmark_ticker.upper()
        for p in positions
        if p.sector_benchmark_ticker
    }
    all_indicator_cache = _market_service.load_indicator_cache_for_tickers(
        uow.session, all_tickers
    )
    all_atr_cache = _market_service.load_atr_cache_for_tickers(
        uow.session, all_tickers
    )
    all_weekly_bars = _market_service.load_weekly_bar_cache_for_tickers(
        uow.session, all_tickers
    )
    all_daily_bars = _market_service.load_daily_bar_cache_for_tickers(
        uow.session, all_tickers | benchmark_tickers
    )

    enriched = [
        _enrich_position(
            p,
            enabled_rules_by_type,
            all_indicator_cache.get(p.ticker),
            all_atr_cache.get(p.ticker),
            all_weekly_bars.get(p.ticker),
            _daily_bars_for_position(p, all_daily_bars),
        )
        for p in positions
    ]
    enriched.sort(key=lambda p: VERDICT_PRIORITY.get(p["verdict"], 99))

    summary = {
        "sell": sum(1 for p in enriched if p["verdict"] == Verdict.sell),
        "trim": sum(1 for p in enriched if p["verdict"] == Verdict.trim),
        "hold": sum(1 for p in enriched if p["verdict"] == Verdict.hold),
        "total": len(enriched),
    }
    any_refresh_in_progress = any(p["refresh_in_progress"] for p in enriched)

    return templates.TemplateResponse(
        request,
        "portfolio.html",
        {
            "positions": enriched,
            "summary": summary,
            "api_configured": get_market_data_api_key() is not None,
            "market_data_provider_name": get_market_data_provider_display_name(),
            "any_refresh_in_progress": any_refresh_in_progress,
            "refresh_poll_timeout_ms": REFRESH_POLL_TIMEOUT_MS,
            "current_user": current_user,
            "flash": _flash_message(request),
        },
    )


@app.get("/add")
def add_position_form(request: Request, uow: UnitOfWork = Depends(get_authenticated_uow)):
    """Show the add-position form."""
    return templates.TemplateResponse(
        request,
        "add_position.html",
        {
            "investment_types": InvestmentType,
            "current_user": _get_current_user(request, uow),
            "error": None,
            "form": None,
        },
    )


@app.get("/rules")
def rules_page(request: Request, uow: UnitOfWork = Depends(get_authenticated_uow)):
    """Show rule configuration for long-term and short-term strategies."""
    user_id = _get_request_user_id(request, uow)
    sections = get_rule_management_sections(uow, user_id=user_id)
    return templates.TemplateResponse(
        request,
        "rules.html",
        {
            "sections": sections,
            "current_user": _get_current_user(request, uow),
        },
    )


@app.post("/rules/{investment_type}/{rule_key}")
def update_rule(
    investment_type: str,
    rule_key: str,
    enabled: str | None = Form(None),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Update enablement for a strategy rule."""
    try:
        update_strategy_rule_config(
            uow=uow,
            investment_type_value=investment_type,
            rule_key=rule_key,
            enabled=enabled is not None,
        )
    except ValueError:
        logger.warning(
            "Invalid rule config update attempted for investment_type=%s rule_key=%s",
            investment_type,
            rule_key,
        )
    return RedirectResponse(url="/rules", status_code=303)


@app.post("/rules/{investment_type}/SELL_MA_ALL/conditions/add")
def add_sell_ma_condition(
    investment_type: str,
    interval: str = Form(...),
    time_period: int = Form(...),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Add an MA condition to the SELL_MA_ALL rule for a strategy."""
    errors = add_ma_condition(uow, investment_type, interval, time_period)
    if errors:
        logger.warning(
            "Failed to add MA condition for %s: %s", investment_type, errors,
        )
    return RedirectResponse(url="/rules", status_code=303)


@app.post("/rules/{investment_type}/SELL_MA_ALL/conditions/delete")
def delete_sell_ma_condition(
    investment_type: str,
    interval: str = Form(...),
    time_period: int = Form(...),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Remove an MA condition from the SELL_MA_ALL rule for a strategy."""
    errors = remove_ma_condition(uow, investment_type, interval, time_period)
    if errors:
        logger.warning(
            "Failed to remove MA condition for %s: %s", investment_type, errors,
        )
    return RedirectResponse(url="/rules", status_code=303)


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------


@app.get("/admin")
def admin_page(request: Request, uow: UnitOfWork = Depends(get_admin_uow)):
    users = [
        {"user": user, "position_count": position_count}
        for user, position_count in uow.users.list_with_position_counts()
    ]
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "users": users,
            "tiers": sorted(TIER_LIMITS),
            "current_user": _get_current_user(request, uow),
            "flash": _flash_message(request),
        },
    )


@app.post("/admin/users/{user_id}/tier")
def admin_update_tier(
    user_id: str,
    tier: str = Form(...),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_admin_uow),
):
    if tier not in TIER_LIMITS:
        raise HTTPException(status_code=400, detail="Unknown tier")
    actor = uow.users.get_by_id(uow.user_id) if uow.user_id else None
    target = uow.users.get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404)
    before = target.tier
    target.tier = tier
    uow.commit()
    logger.warning(
        "Admin user mutation",
        extra={
            "actor_id": actor.id if actor else None,
            "actor_email": actor.email if actor else None,
            "target_id": target.id,
            "target_email": target.email,
            "action": "update_tier",
            "before": before,
            "after": tier,
        },
    )
    return _admin_redirect_with_flash()


@app.post("/admin/users/{user_id}/admin")
def admin_update_admin_flag(
    user_id: str,
    is_admin: bool = Form(False),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_admin_uow),
):
    actor = uow.users.get_by_id(uow.user_id) if uow.user_id else None
    target = uow.users.get_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404)
    before = bool(target.is_admin)
    if before and not is_admin and uow.users.count_admins(for_update=True) <= 1:
        raise HTTPException(status_code=400, detail="Cannot remove the last admin")
    target.is_admin = is_admin
    uow.commit()
    logger.warning(
        "Admin user mutation",
        extra={
            "actor_id": actor.id if actor else None,
            "actor_email": actor.email if actor else None,
            "target_id": target.id,
            "target_email": target.email,
            "action": "update_admin",
            "before": before,
            "after": is_admin,
        },
    )
    return _admin_redirect_with_flash()


@app.get("/api/lookup/{ticker}")
def lookup_ticker(
    ticker: str,
    _authenticated_uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Look up ticker matches and the latest price via the configured provider.

    Dependency injection of ``_authenticated_uow`` enforces authentication before
    allowing access to this quota-consuming endpoint.
    """
    api_key = get_market_data_api_key()
    if not api_key:
        return JSONResponse(
            status_code=503,
            content={"error": "Market data API key is not configured"},
        )
    clean_ticker = ticker.strip().upper()
    try:
        matches = _market_service.fetch_ticker_matches(clean_ticker)
        current_price = None
        try:
            bars = _market_service.fetch_daily_series(clean_ticker)
            if bars:
                current_price = bars[0].close
        except Exception:
            logger.info("Price lookup unavailable for %s", clean_ticker, exc_info=True)

        return {
            "company_name": matches[0].name,
            "current_price": current_price,
            "matches": [
                {
                    "symbol": match.symbol,
                    "name": match.name,
                    "region": match.region,
                    "type": match.type,
                    "match_score": match.match_score,
                }
                for match in matches
            ],
        }
    except MarketDataSymbolNotFound:
        return JSONResponse(
            status_code=404,
            content={"error": f"No results found for {clean_ticker}"},
        )
    except MarketDataError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": "Company name lookup failed"},
        )
    except Exception:
        return JSONResponse(
            status_code=502,
            content={"error": "Company name lookup failed"},
        )


@app.post("/add")
def add_position(
    request: Request,
    background_tasks: BackgroundTasks,
    ticker: str = Form(...),
    company_name: str = Form(...),
    cost_basis: float = Form(...),
    initial_purchase_date: str = Form(...),
    investment_type: str = Form(...),
    notes: str = Form(""),
    sector_benchmark_ticker: str = Form(""),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Create a new position and redirect to portfolio.

    The latest closing price is fetched synchronously from the configured provider.
    A background task is then queued to fetch the remaining market data
    (SMA values and weekly data for long-term positions).
    """
    user_id = _get_request_user_id(request, uow)
    current_user = _get_current_user(request, uow)
    if current_user is None:
        raise HTTPException(status_code=401)
    try:
        check_can_add_position(current_user, uow.positions.count_all())
        check_and_consume_refresh(current_user)
    except TierLimitExceeded as exc:
        return templates.TemplateResponse(
            request,
            "add_position.html",
            {
                "investment_types": InvestmentType,
                "current_user": current_user,
                "error": exc.message,
                "form": {
                    "ticker": ticker,
                    "company_name": company_name,
                    "cost_basis": cost_basis,
                    "initial_purchase_date": initial_purchase_date,
                    "investment_type": investment_type,
                    "notes": notes,
                    "sector_benchmark_ticker": sector_benchmark_ticker,
                },
            },
            status_code=200,
        )
    clean_ticker = ticker.strip().upper()
    clean_benchmark = sector_benchmark_ticker.strip().upper() or None
    current_price = 0.0
    daily_close = None
    daily_market_date = None
    daily_retrieved_at = None

    api_key = get_market_data_api_key()
    if api_key:
        try:
            bars = _market_service.fetch_daily_series(clean_ticker)
            if bars:
                current_price = bars[0].close
                daily_close = bars[0].close
                daily_market_date = bars[0].date
                daily_retrieved_at = datetime.now()
        except Exception:
            logger.warning(
                "Failed to fetch price for %s from %s",
                clean_ticker,
                get_market_data_provider_display_name(),
                exc_info=True,
            )

    pos = Position(
        ticker=clean_ticker,
        company_name=company_name.strip(),
        cost_basis=cost_basis,
        initial_purchase_date=date.fromisoformat(initial_purchase_date),
        investment_type=investment_type,
        current_price=current_price,
        daily_close=daily_close,
        daily_market_date=daily_market_date,
        daily_retrieved_at=daily_retrieved_at,
        notes=notes.strip() or None,
        sector_benchmark_ticker=clean_benchmark,
        user_id=user_id,
    )
    uow.positions.add(pos)
    uow.commit()
    logger.info("Created position %s (%s) — cost_basis=%.2f, type=%s", clean_ticker, company_name.strip(), cost_basis, investment_type)

    # Queue a background refresh to fetch SMA and weekly data
    if api_key:
        uow.positions.refresh_instance(pos)
        _mark_positions_refresh_state(uow, [pos.id], in_progress=True)
        background_tasks.add_task(_refresh_single_position_task, pos.id, user_id)

    return RedirectResponse(url="/", status_code=303)


@app.get("/edit/{position_id}")
def edit_position_form(position_id: int, request: Request, uow: UnitOfWork = Depends(get_authenticated_uow)):
    """Show the edit form for an existing position."""
    pos = uow.positions.get_by_id(position_id)
    if not pos:
        return RedirectResponse(url="/", status_code=303)
    effective_current_price = pos.daily_close if pos.daily_close is not None else pos.current_price
    return templates.TemplateResponse(
        request,
        "edit_position.html",
        {
            "position": pos,
            "effective_current_price": effective_current_price,
            "investment_types": InvestmentType,
            "current_user": _get_current_user(request, uow),
        },
    )


@app.post("/edit/{position_id}")
def edit_position(
    background_tasks: BackgroundTasks,
    position_id: int,
    ticker: str = Form(...),
    company_name: str = Form(...),
    cost_basis: float = Form(...),
    initial_purchase_date: str = Form(...),
    investment_type: str = Form(...),
    current_price: float = Form(...),
    notes: str = Form(""),
    sector_benchmark_ticker: str = Form(""),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Update an existing position and redirect to portfolio.

    If the ticker changes, cached market data is cleared and refreshed.
    """
    pos = uow.positions.get_by_id(position_id)
    if not pos:
        return RedirectResponse(url="/", status_code=303)
    clean_ticker = ticker.strip().upper()
    clean_company_name = company_name.strip()
    clean_benchmark = sector_benchmark_ticker.strip().upper() or None
    ticker_changed = pos.ticker != clean_ticker
    effective_current_price = pos.daily_close if pos.daily_close is not None else pos.current_price
    submitted_current_price = current_price
    if (
        not ticker_changed
        and pos.daily_close is not None
        and isclose(
            submitted_current_price,
            effective_current_price,
            abs_tol=PRICE_COMPARISON_ABS_TOLERANCE,
        )
    ):
        submitted_current_price = pos.current_price
    if ticker_changed:
        _clear_position_market_data(pos)
    pos.ticker = clean_ticker
    pos.company_name = clean_company_name
    pos.cost_basis = cost_basis
    pos.initial_purchase_date = date.fromisoformat(initial_purchase_date)
    pos.investment_type = investment_type
    pos.current_price = submitted_current_price
    pos.notes = notes.strip() or None
    pos.sector_benchmark_ticker = clean_benchmark
    uow.commit()
    if ticker_changed and get_market_data_api_key():
        _mark_positions_refresh_state(uow, [pos.id], in_progress=True)
        background_tasks.add_task(_refresh_single_position_task, pos.id, uow.user_id)
    logger.info("Updated position id=%d %s — current_price=%.2f", position_id, pos.ticker, submitted_current_price)
    return RedirectResponse(url="/", status_code=303)


@app.post("/edit/{position_id}/key-levels/add")
def add_key_level(
    position_id: int,
    level_price: float = Form(...),
    label: str = Form(""),
    notes: str = Form(""),
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Add a manually-identified key level to a position (issue #23)."""
    pos = uow.positions.get_by_id(position_id)
    if not pos:
        return RedirectResponse(url="/", status_code=303)
    if level_price > 0:
        kl = PositionKeyLevel(
            position_id=pos.id,
            level_price=level_price,
            label=label.strip() or None,
            notes=notes.strip() or None,
            is_active=True,
        )
        uow.key_levels.add(kl)
        uow.commit()
        logger.info(
            "Added key level $%.2f for position id=%d %s",
            level_price, position_id, pos.ticker,
        )
    return RedirectResponse(url=f"/edit/{pos.id}", status_code=303)


@app.post("/edit/{position_id}/key-levels/{level_id}/delete")
def delete_key_level(
    position_id: int,
    level_id: int,
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Delete a key level from a position."""
    kl = uow.key_levels.get_by_position_and_id(position_id, level_id)
    if kl:
        uow.key_levels.delete(kl)
        uow.commit()
        logger.info("Deleted key level id=%d for position id=%d", level_id, position_id)
        return RedirectResponse(url=f"/edit/{kl.position_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.post("/edit/{position_id}/key-levels/{level_id}/toggle")
def toggle_key_level(
    position_id: int,
    level_id: int,
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Toggle the is_active flag on a key level."""
    kl = uow.key_levels.get_by_position_and_id(position_id, level_id)
    if kl:
        kl.is_active = not kl.is_active
        uow.commit()
        return RedirectResponse(url=f"/edit/{kl.position_id}", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{position_id}")
def delete_position(
    position_id: int,
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Delete a position and redirect to portfolio."""
    pos = uow.positions.get_by_id(position_id)
    if pos:
        logger.info("Deleted position id=%d %s", position_id, pos.ticker)
        uow.positions.delete(pos)
        uow.commit()
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Market data refresh routes
# ---------------------------------------------------------------------------


def _refresh_all_positions_task(position_ids: list[int], user_id: str):
    """Run a full market data refresh in the background with its own DB session."""
    uow = SqlAlchemyUnitOfWork(SessionLocal(), user_id=user_id)
    try:
        try:
            _market_service.refresh_all_positions(uow.session, user_id=user_id)
        except Exception as exc:
            logger.warning("Background refresh-all failed", exc_info=True)
            detail = str(exc).strip() or exc.__class__.__name__
            uow.rollback()
            for pos in uow.positions.get_by_ids(position_ids):
                pos.refresh_error = f"Refresh failed: {detail}"
            uow.commit()
    finally:
        _mark_positions_refresh_state(uow, position_ids, in_progress=False)
        uow.session.close()


def _refresh_single_position_task(position_id: int, user_id: str):
    """Run a single-position market data refresh in the background with its own DB session."""
    uow = SqlAlchemyUnitOfWork(SessionLocal(), user_id=user_id)
    pos = None
    try:
        _mark_positions_refresh_state(uow, [position_id], in_progress=True)
        try:
            pos = uow.positions.get_by_id(position_id)
            if pos:
                _market_service.refresh_position(pos, uow.session)
        except Exception as exc:
            logger.warning(
                "Background refresh failed for position id=%d", position_id, exc_info=True
            )
            if pos is not None:
                detail = str(exc).strip() or exc.__class__.__name__
                uow.rollback()
                pos = uow.positions.get_by_id(position_id)
                if pos is not None:
                    pos.refresh_error = f"Refresh failed: {detail}"
                    uow.commit()
    finally:
        _mark_positions_refresh_state(uow, [position_id], in_progress=False)
        uow.session.close()


@app.post("/refresh")
def refresh_all(
    background_tasks: BackgroundTasks,
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Refresh cached market data for all positions (respects staleness checks)."""
    _clear_stale_refresh_flags(uow)
    if uow.positions.has_any_refresh_in_progress():
        return RedirectResponse(url="/", status_code=303)

    position_ids = uow.positions.list_all_ids()
    if position_ids:
        current_user = uow.users.get_by_id(uow.user_id) if uow.user_id else None
        if current_user is None:
            raise HTTPException(status_code=401)
        try:
            check_and_consume_refresh(current_user)
            uow.commit()
        except TierLimitExceeded:
            uow.rollback()
            return _redirect_with_refresh_limit_flash()
        _mark_positions_refresh_state(uow, position_ids, in_progress=True)
        background_tasks.add_task(_refresh_all_positions_task, position_ids, uow.user_id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/refresh/{position_id}")
def refresh_single(
    background_tasks: BackgroundTasks,
    position_id: int,
    _csrf: None = Depends(validate_csrf),
    uow: UnitOfWork = Depends(get_authenticated_uow),
):
    """Refresh cached market data for a single position in the background."""
    _clear_stale_refresh_flags(uow)
    pos = uow.positions.get_by_id(position_id)
    if pos and not pos.refresh_in_progress:
        current_user = uow.users.get_by_id(uow.user_id) if uow.user_id else None
        if current_user is None:
            raise HTTPException(status_code=401)
        try:
            check_and_consume_refresh(current_user)
            uow.commit()
        except TierLimitExceeded:
            uow.rollback()
            return _redirect_with_refresh_limit_flash()
        _mark_positions_refresh_state(uow, [position_id], in_progress=True)
        background_tasks.add_task(_refresh_single_position_task, position_id, uow.user_id)
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/refresh-status")
def refresh_status(uow: UnitOfWork = Depends(get_authenticated_uow)):
    """Expose lightweight in-progress refresh state for client-side polling."""
    _clear_stale_refresh_flags(uow)
    positions = uow.positions.list_refresh_statuses()
    return {
        "any_in_progress": any(bool(in_progress) for _, in_progress, _ in positions),
        "positions": [
            {
                "id": position_id,
                "in_progress": bool(in_progress),
                "started_at": (
                    started_at.isoformat() if started_at else None
                ),
            }
            for position_id, in_progress, started_at in positions
        ],
    }
