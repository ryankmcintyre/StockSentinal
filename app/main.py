import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
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
from app.database import get_db, init_db
from app.market_data import (
    fetch_company_name,
    fetch_daily_series,
    refresh_all_positions,
    refresh_position,
)
from app.models import Position
from app.rule_engine import (
    MarketSignals,
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

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Stock Sentinel — initializing database")
    init_db()
    logger.info("Database initialized, application ready")
    yield


app = FastAPI(title="Stock Sentinel", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VERDICT_PRIORITY = {Verdict.sell: 0, Verdict.trim: 1, Verdict.hold: 2}


def _enrich_position(pos: Position) -> dict:
    """Run rule engine on a Position and return a dict with all display fields."""
    # Build market signals from cached Alpha Vantage data
    signals = MarketSignals(
        daily_close=pos.daily_close,
        daily_sma_21=pos.daily_sma_21,
        weekly_close=pos.weekly_close,
        weekly_sma_20=pos.weekly_sma_20,
    )

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

    triggered = evaluate_position(eval_pos, signals=signals)
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
        "has_market_data": pos.daily_close is not None or pos.weekly_close is not None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/")
def portfolio(request: Request, db: Session = Depends(get_db)):
    """Dashboard: list all positions with verdicts, sorted by urgency."""
    positions = db.query(Position).all()
    enriched = [_enrich_position(p) for p in positions]
    enriched.sort(key=lambda p: VERDICT_PRIORITY.get(p["verdict"], 99))

    summary = {
        "sell": sum(1 for p in enriched if p["verdict"] == Verdict.sell),
        "trim": sum(1 for p in enriched if p["verdict"] == Verdict.trim),
        "hold": sum(1 for p in enriched if p["verdict"] == Verdict.hold),
        "total": len(enriched),
    }

    return templates.TemplateResponse(
        request,
        "portfolio.html",
        {
            "positions": enriched,
            "summary": summary,
            "api_configured": get_alpha_vantage_api_key() is not None,
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
    ticker: str = Form(...),
    company_name: str = Form(...),
    cost_basis: float = Form(...),
    initial_purchase_date: str = Form(...),
    investment_type: str = Form(...),
    notes: str = Form(""),
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Create a new position and redirect to portfolio.

    The latest closing price is fetched synchronously from Alpha Vantage.
    A background task is then queued to fetch the remaining market data
    (SMA values and weekly data for long-term positions).
    """
    clean_ticker = ticker.strip().upper()
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
    )
    db.add(pos)
    db.commit()
    logger.info("Created position %s (%s) — cost_basis=%.2f, type=%s", clean_ticker, company_name.strip(), cost_basis, investment_type)

    # Queue a background refresh to fetch SMA and weekly data
    if api_key:
        db.refresh(pos)
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
    ticker: str = Form(...),
    company_name: str = Form(...),
    cost_basis: float = Form(...),
    initial_purchase_date: str = Form(...),
    investment_type: str = Form(...),
    current_price: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Update an existing position and redirect to portfolio."""
    pos = db.query(Position).filter(Position.id == position_id).first()
    if not pos:
        return RedirectResponse(url="/", status_code=303)
    pos.ticker = ticker.strip().upper()
    pos.company_name = company_name.strip()
    pos.cost_basis = cost_basis
    pos.initial_purchase_date = date.fromisoformat(initial_purchase_date)
    pos.investment_type = investment_type
    pos.current_price = current_price
    pos.notes = notes.strip() or None
    db.commit()
    logger.info("Updated position id=%d %s — current_price=%.2f", position_id, pos.ticker, current_price)
    return RedirectResponse(url="/", status_code=303)


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


def _refresh_all_positions_task():
    """Run a full market data refresh in the background with its own DB session."""
    db_generator = get_db()
    db = next(db_generator)
    try:
        refresh_all_positions(db)
    finally:
        db_generator.close()


def _refresh_single_position_task(position_id: int):
    """Run a single-position market data refresh in the background with its own DB session."""
    db_generator = get_db()
    db = next(db_generator)
    try:
        pos = db.query(Position).filter(Position.id == position_id).first()
        if pos:
            refresh_position(pos, db)
    finally:
        db_generator.close()


@app.post("/refresh")
def refresh_all(background_tasks: BackgroundTasks):
    """Refresh cached market data for all positions (respects staleness checks)."""
    background_tasks.add_task(_refresh_all_positions_task)
    return RedirectResponse(url="/", status_code=303)


@app.post("/refresh/{position_id}")
def refresh_single(
    position_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Refresh cached market data for a single position."""
    pos = db.query(Position).filter(Position.id == position_id).first()
    if pos:
        background_tasks.add_task(_refresh_single_position_task, position_id)
    return RedirectResponse(url="/", status_code=303)
