from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.models import Position
from app.rule_engine import (
    compute_hold_duration_days,
    compute_percent_gain,
    evaluate_position,
    get_verdict,
)
from app.schemas import InvestmentType, Verdict

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
    triggered = evaluate_position(pos)
    verdict = get_verdict(triggered)
    return {
        "id": pos.id,
        "ticker": pos.ticker,
        "company_name": pos.company_name,
        "cost_basis": pos.cost_basis,
        "current_price": pos.current_price,
        "investment_type": pos.investment_type,
        "initial_purchase_date": pos.initial_purchase_date,
        "notes": pos.notes,
        "percent_gain": compute_percent_gain(pos.cost_basis, pos.current_price),
        "hold_duration_days": compute_hold_duration_days(pos.initial_purchase_date),
        "verdict": verdict,
        "triggered_rules": triggered,
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
        {"positions": enriched, "summary": summary},
    )


@app.get("/add")
def add_position_form(request: Request):
    """Show the add-position form."""
    return templates.TemplateResponse(
        request,
        "add_position.html",
        {"investment_types": InvestmentType},
    )


@app.post("/add")
def add_position(
    ticker: str = Form(...),
    company_name: str = Form(...),
    cost_basis: float = Form(...),
    initial_purchase_date: str = Form(...),
    investment_type: str = Form(...),
    current_price: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a new position and redirect to portfolio."""
    pos = Position(
        ticker=ticker.strip().upper(),
        company_name=company_name.strip(),
        cost_basis=cost_basis,
        initial_purchase_date=date.fromisoformat(initial_purchase_date),
        investment_type=investment_type,
        current_price=current_price,
        notes=notes.strip() or None,
    )
    db.add(pos)
    db.commit()
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
    return RedirectResponse(url="/", status_code=303)


@app.post("/delete/{position_id}")
def delete_position(position_id: int, db: Session = Depends(get_db)):
    """Delete a position and redirect to portfolio."""
    pos = db.query(Position).filter(Position.id == position_id).first()
    if pos:
        db.delete(pos)
        db.commit()
    return RedirectResponse(url="/", status_code=303)
