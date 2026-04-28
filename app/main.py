import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

load_dotenv()

from app.alpha_vantage_client import (
    AlphaVantageError,
)
from app.config import get_alpha_vantage_api_key, get_log_level
from app.database import SessionLocal, get_db, init_db
from app.market_data import (
    fetch_company_name,
    fetch_daily_series,
    load_atr_cache_for_tickers,
    load_daily_bar_cache_for_tickers,
    load_indicator_cache_for_tickers,
    load_weekly_bar_cache_for_tickers,
    refresh_all_positions,
    refresh_position,
)
from app.models import Position, PositionKeyLevel
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

log_level = get_log_level()
logging.basicConfig(
    level=log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger().setLevel(log_level)

# Suppress third-party HTTP loggers that leak sensitive query parameters
# (e.g. urllib3 logs full request URLs including the Alpha Vantage API key)
_HTTP_LOGGERS = ("urllib3", "requests", "httpcore", "httpx", "http.client")
for _name in _HTTP_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Stock Sentinel — initializing database")
    init_db()
    db = SessionLocal()
    try:
        cleared = _clear_stale_refresh_flags(db)
        if cleared:
            logger.info("Cleared %d stale refresh-in-progress flags", cleared)
    finally:
        db.close()
    logger.info("Database initialized, application ready")
    yield


app = FastAPI(title="Stock Sentinel", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VERDICT_PRIORITY = {Verdict.sell: 0, Verdict.trim: 1, Verdict.hold: 2}
REFRESH_STALE_TIMEOUT_MINUTES = 5


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
    db: Session,
    position_ids: list[int],
    *,
    in_progress: bool,
) -> int:
    """Set refresh status fields for one or more positions and commit."""
    if not position_ids:
        return 0

    positions = db.query(Position).filter(Position.id.in_(position_ids)).all()
    now = datetime.now()
    for pos in positions:
        pos.refresh_in_progress = in_progress
        pos.refresh_started_at = now if in_progress else None
        if in_progress:
            pos.refresh_error = None
    db.commit()
    return len(positions)


def _clear_stale_refresh_flags(db: Session) -> int:
    """Reset stale in-progress flags left behind by interrupted refreshes."""
    cutoff = datetime.now() - timedelta(minutes=REFRESH_STALE_TIMEOUT_MINUTES)
    stale_positions = (
        db.query(Position)
        .filter(Position.refresh_in_progress.is_(True))
        .filter(Position.refresh_started_at.is_not(None))
        .filter(Position.refresh_started_at < cutoff)
        .all()
    )
    if not stale_positions:
        return 0

    for pos in stale_positions:
        pos.refresh_in_progress = False
        pos.refresh_started_at = None
    db.commit()
    return len(stale_positions)


def _enrich_position(
    pos: Position,
    enabled_rules_by_type: dict[str, list[StrategyRuleSelection]] | None = None,
    indicator_cache: dict[tuple[str, int], tuple[float | None, float | None]] | None = None,
    atr_cache: dict[tuple[str, int], float | None] | None = None,
    weekly_bars: list | None = None,
    daily_bars_by_ticker: dict[str, list] | None = None,
) -> dict:
    """Run rule engine on a Position and return a dict with all display fields."""
    # Build market signals from cached Alpha Vantage data
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
        "triggered_rules": triggered,
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


@app.get("/")
def portfolio(request: Request, db: Session = Depends(get_db)):
    """Dashboard: list all positions with verdicts, sorted by urgency."""
    _clear_stale_refresh_flags(db)
    positions = db.query(Position).all()
    enabled_rules_by_type = get_enabled_rule_selections_by_investment_type(db)

    # Preload indicator cache for all tickers to avoid N+1 queries
    all_tickers = {p.ticker for p in positions}
    benchmark_tickers = {
        p.sector_benchmark_ticker.upper()
        for p in positions
        if p.sector_benchmark_ticker
    }
    all_indicator_cache = load_indicator_cache_for_tickers(db, all_tickers)
    all_atr_cache = load_atr_cache_for_tickers(db, all_tickers)
    all_weekly_bars = load_weekly_bar_cache_for_tickers(db, all_tickers)
    all_daily_bars = load_daily_bar_cache_for_tickers(
        db, all_tickers | benchmark_tickers
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
            "api_configured": get_alpha_vantage_api_key() is not None,
            "any_refresh_in_progress": any_refresh_in_progress,
        },
    )


@app.get("/add")
def add_position_form(request: Request):
    """Show the add-position form."""
    return templates.TemplateResponse(
        request,
        "add_position.html",
        {"investment_types": InvestmentType},
    )


@app.get("/rules")
def rules_page(request: Request, db: Session = Depends(get_db)):
    """Show rule configuration for long-term and short-term strategies."""
    sections = get_rule_management_sections(db)
    return templates.TemplateResponse(
        request,
        "rules.html",
        {"sections": sections},
    )


@app.post("/rules/{investment_type}/{rule_key}")
def update_rule(
    investment_type: str,
    rule_key: str,
    enabled: str | None = Form(None),
    db: Session = Depends(get_db),
):
    """Update enablement for a strategy rule."""
    try:
        update_strategy_rule_config(
            db=db,
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
    db: Session = Depends(get_db),
):
    """Add an MA condition to the SELL_MA_ALL rule for a strategy."""
    errors = add_ma_condition(db, investment_type, interval, time_period)
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
    db: Session = Depends(get_db),
):
    """Remove an MA condition from the SELL_MA_ALL rule for a strategy."""
    errors = remove_ma_condition(db, investment_type, interval, time_period)
    if errors:
        logger.warning(
            "Failed to remove MA condition for %s: %s", investment_type, errors,
        )
    return RedirectResponse(url="/rules", status_code=303)


@app.get("/api/lookup/{ticker}")
def lookup_ticker(ticker: str):
    """Look up the company name for a ticker symbol via Alpha Vantage."""
    api_key = get_alpha_vantage_api_key()
    if not api_key:
        return JSONResponse(
            status_code=503,
            content={"error": "Alpha Vantage API key is not configured"},
        )
    try:
        company_name = fetch_company_name(ticker.strip().upper(), api_key)
        return {"company_name": company_name}
    except AlphaVantageError as exc:
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
    background_tasks: BackgroundTasks,
    ticker: str = Form(...),
    company_name: str = Form(...),
    cost_basis: float = Form(...),
    initial_purchase_date: str = Form(...),
    investment_type: str = Form(...),
    notes: str = Form(""),
    sector_benchmark_ticker: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a new position and redirect to portfolio.

    The latest closing price is fetched synchronously from Alpha Vantage.
    A background task is then queued to fetch the remaining market data
    (SMA values and weekly data for long-term positions).
    """
    clean_ticker = ticker.strip().upper()
    clean_benchmark = sector_benchmark_ticker.strip().upper() or None
    current_price = 0.0
    daily_close = None
    daily_market_date = None
    daily_retrieved_at = None

    api_key = get_alpha_vantage_api_key()
    if api_key:
        try:
            bars = fetch_daily_series(clean_ticker, api_key)
            if bars:
                current_price = bars[0].close
                daily_close = bars[0].close
                daily_market_date = bars[0].date
                daily_retrieved_at = datetime.now()
        except Exception:
            logger.warning("Failed to fetch price for %s from Alpha Vantage", clean_ticker, exc_info=True)

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
    )
    db.add(pos)
    db.commit()
    logger.info("Created position %s (%s) — cost_basis=%.2f, type=%s", clean_ticker, company_name.strip(), cost_basis, investment_type)

    # Queue a background refresh to fetch SMA and weekly data
    if api_key:
        db.refresh(pos)
        _mark_positions_refresh_state(db, [pos.id], in_progress=True)
        background_tasks.add_task(_refresh_single_position_task, pos.id)

    return RedirectResponse(url="/", status_code=303)


@app.get("/edit/{position_id}")
def edit_position_form(position_id: int, request: Request, db: Session = Depends(get_db)):
    """Show the edit form for an existing position."""
    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request,
        "edit_position.html",
        {"position": pos, "investment_types": InvestmentType},
    )


@app.post("/edit/{position_id}")
def edit_position(
    position_id: int,
    cost_basis: float = Form(...),
    initial_purchase_date: str = Form(...),
    investment_type: str = Form(...),
    current_price: float = Form(...),
    notes: str = Form(""),
    sector_benchmark_ticker: str = Form(""),
    db: Session = Depends(get_db),
):
    """Update an existing position and redirect to portfolio.

    Ticker and company name are immutable after creation and are not
    accepted from the form.
    """
    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        return RedirectResponse(url="/", status_code=303)
    pos.cost_basis = cost_basis
    pos.initial_purchase_date = date.fromisoformat(initial_purchase_date)
    pos.investment_type = investment_type
    pos.current_price = current_price
    pos.notes = notes.strip() or None
    pos.sector_benchmark_ticker = sector_benchmark_ticker.strip().upper() or None
    db.commit()
    logger.info("Updated position id=%d %s — current_price=%.2f", position_id, pos.ticker, current_price)
    return RedirectResponse(url="/", status_code=303)


@app.post("/edit/{position_id}/key-levels/add")
def add_key_level(
    position_id: int,
    level_price: float = Form(...),
    label: str = Form(""),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Add a manually-identified key level to a position (issue #23)."""
    pos = db.query(Position).filter(Position.id == position_id).first()
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
        db.add(kl)
        db.commit()
        logger.info(
            "Added key level $%.2f for position id=%d %s",
            level_price, position_id, pos.ticker,
        )
    return RedirectResponse(url=f"/edit/{position_id}", status_code=303)


@app.post("/edit/{position_id}/key-levels/{level_id}/delete")
def delete_key_level(position_id: int, level_id: int, db: Session = Depends(get_db)):
    """Delete a key level from a position."""
    kl = (
        db.query(PositionKeyLevel)
        .filter(PositionKeyLevel.id == level_id)
        .filter(PositionKeyLevel.position_id == position_id)
        .first()
    )
    if kl:
        db.delete(kl)
        db.commit()
        logger.info("Deleted key level id=%d for position id=%d", level_id, position_id)
    return RedirectResponse(url=f"/edit/{position_id}", status_code=303)


@app.post("/edit/{position_id}/key-levels/{level_id}/toggle")
def toggle_key_level(position_id: int, level_id: int, db: Session = Depends(get_db)):
    """Toggle the is_active flag on a key level."""
    kl = (
        db.query(PositionKeyLevel)
        .filter(PositionKeyLevel.id == level_id)
        .filter(PositionKeyLevel.position_id == position_id)
        .first()
    )
    if kl:
        kl.is_active = not kl.is_active
        db.commit()
    return RedirectResponse(url=f"/edit/{position_id}", status_code=303)


@app.post("/delete/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db)):
    """Delete a position and redirect to portfolio."""
    pos = db.query(Position).filter(Position.id == position_id).first()
    if pos:
        logger.info("Deleted position id=%d %s", position_id, pos.ticker)
        db.delete(pos)
        db.commit()
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# Market data refresh routes
# ---------------------------------------------------------------------------


def _refresh_all_positions_task(position_ids: list[int]):
    """Run a full market data refresh in the background with its own DB session."""
    db_generator = get_db()
    db = next(db_generator)
    try:
        try:
            refresh_all_positions(db)
        except Exception as exc:
            logger.warning("Background refresh-all failed", exc_info=True)
            detail = str(exc).strip() or exc.__class__.__name__
            db.rollback()
            for pos in db.query(Position).filter(Position.id.in_(position_ids)).all():
                pos.refresh_error = f"Refresh failed: {detail}"
            db.commit()
    finally:
        _mark_positions_refresh_state(db, position_ids, in_progress=False)
        db_generator.close()


def _refresh_single_position_task(position_id: int):
    """Run a single-position market data refresh in the background with its own DB session."""
    db_generator = get_db()
    db = next(db_generator)
    pos = None
    try:
        _mark_positions_refresh_state(db, [position_id], in_progress=True)
        try:
            pos = db.query(Position).filter(Position.id == position_id).first()
            if pos:
                refresh_position(pos, db)
        except Exception as exc:
            logger.warning(
                "Background refresh failed for position id=%d", position_id, exc_info=True
            )
            if pos is not None:
                detail = str(exc).strip() or exc.__class__.__name__
                db.rollback()
                pos = db.query(Position).filter(Position.id == position_id).first()
                if pos is not None:
                    pos.refresh_error = f"Refresh failed: {detail}"
                    db.commit()
    finally:
        _mark_positions_refresh_state(db, [position_id], in_progress=False)
        db_generator.close()


@app.post("/refresh")
def refresh_all(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Refresh cached market data for all positions (respects staleness checks)."""
    _clear_stale_refresh_flags(db)
    if db.query(Position).filter(Position.refresh_in_progress.is_(True)).first():
        return RedirectResponse(url="/", status_code=303)

    position_ids = [pos_id for (pos_id,) in db.query(Position.id).all()]
    if position_ids:
        _mark_positions_refresh_state(db, position_ids, in_progress=True)
        background_tasks.add_task(_refresh_all_positions_task, position_ids)
    return RedirectResponse(url="/", status_code=303)


@app.post("/refresh/{position_id}")
def refresh_single(
    position_id: int,
    db: Session = Depends(get_db),
):
    """Refresh cached market data for a single position inline.

    Running this inline avoids a race where the redirect can render before
    background work writes refresh_error.
    """
    _clear_stale_refresh_flags(db)
    pos = db.query(Position).filter(Position.id == position_id).first()
    if pos and not pos.refresh_in_progress:
        _mark_positions_refresh_state(db, [position_id], in_progress=True)
        try:
            refresh_position(pos, db)
        except Exception as exc:
            logger.warning(
                "Inline refresh failed for position id=%d", position_id, exc_info=True
            )
            detail = str(exc).strip() or exc.__class__.__name__
            db.rollback()
            pos = db.query(Position).filter(Position.id == position_id).first()
            if pos is not None:
                pos.refresh_error = f"Refresh failed: {detail}"
                db.commit()
        finally:
            _mark_positions_refresh_state(db, [position_id], in_progress=False)
    return RedirectResponse(url="/", status_code=303)


@app.get("/api/refresh-status")
def refresh_status(db: Session = Depends(get_db)):
    """Expose lightweight in-progress refresh state for client-side polling."""
    _clear_stale_refresh_flags(db)
    positions = db.query(Position).all()
    return {
        "any_in_progress": any(bool(pos.refresh_in_progress) for pos in positions),
        "positions": [
            {
                "id": pos.id,
                "in_progress": bool(pos.refresh_in_progress),
                "started_at": (
                    pos.refresh_started_at.isoformat() if pos.refresh_started_at else None
                ),
            }
            for pos in positions
        ],
    }
