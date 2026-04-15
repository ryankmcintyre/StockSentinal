from datetime import date

from sqlalchemy import Column, Date, Float, Integer, String
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
