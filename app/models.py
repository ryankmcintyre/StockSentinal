from datetime import date, datetime

from sqlalchemy import Column, Date, DateTime, Float, Integer, String
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Position(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, nullable=False)
    company_name = Column(String, nullable=False)
    cost_basis = Column(Float, nullable=False)
    initial_purchase_date = Column(Date, nullable=False)
    investment_type = Column(String, nullable=False)  # "long-term" or "short-term"
    current_price = Column(Float, nullable=False)
    notes = Column(String, nullable=True)

    # --- Cached market data from Alpha Vantage ---
    # Daily data (used by short-term sell rule and trim/hold)
    daily_close = Column(Float, nullable=True)
    daily_sma_21 = Column(Float, nullable=True)
    daily_market_date = Column(Date, nullable=True)
    daily_retrieved_at = Column(DateTime, nullable=True)

    # Weekly data (used by long-term sell rule)
    weekly_close = Column(Float, nullable=True)
    weekly_sma_20 = Column(Float, nullable=True)
    weekly_market_date = Column(Date, nullable=True)
    weekly_retrieved_at = Column(DateTime, nullable=True)

    # Refresh status
    refresh_error = Column(String, nullable=True)
