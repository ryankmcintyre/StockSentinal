from datetime import date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class InvestmentType(str, Enum):
    long_term = "long-term"
    short_term = "short-term"


class Verdict(str, Enum):
    sell = "Sell"
    trim = "Trim"
    hold = "Hold"


class PositionBase(BaseModel):
    """Base schema for position data shared across create/update/response."""
    ticker: str = Field(..., min_length=1, max_length=10)
    company_name: str = Field(..., min_length=1)
    cost_basis: float = Field(..., gt=0)
    initial_purchase_date: date
    investment_type: InvestmentType
    current_price: float = Field(..., gt=0)
    notes: Optional[str] = None
    sector_benchmark_ticker: Optional[str] = Field(None, max_length=10)

    @field_validator("ticker")
    @classmethod
    def ticker_uppercase(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("sector_benchmark_ticker")
    @classmethod
    def benchmark_uppercase(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        return v or None

    @field_validator("initial_purchase_date")
    @classmethod
    def purchase_date_not_in_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Purchase date cannot be in the future")
        return v


class PositionCreate(PositionBase):
    """Schema for creating a new position.

    current_price is optional because it is fetched automatically from
    the configured market data provider when a position is added.
    """
    current_price: Optional[float] = Field(None, gt=0)


class PositionUpdate(BaseModel):
    """Schema for updating an existing position. All fields optional."""
    ticker: Optional[str] = Field(None, min_length=1, max_length=10)
    company_name: Optional[str] = Field(None, min_length=1)
    cost_basis: Optional[float] = Field(None, gt=0)
    initial_purchase_date: Optional[date] = None
    investment_type: Optional[InvestmentType] = None
    current_price: Optional[float] = Field(None, gt=0)
    notes: Optional[str] = None
    sector_benchmark_ticker: Optional[str] = Field(None, max_length=10)

    @field_validator("ticker")
    @classmethod
    def ticker_uppercase(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return v.strip().upper()
        return v

    @field_validator("sector_benchmark_ticker")
    @classmethod
    def benchmark_uppercase(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip().upper()
        return v or None

    @field_validator("initial_purchase_date")
    @classmethod
    def purchase_date_not_in_future(cls, v: Optional[date]) -> Optional[date]:
        if v is not None and v > date.today():
            raise ValueError("Purchase date cannot be in the future")
        return v


class RuleResult(BaseModel):
    """Result from a single rule evaluation."""
    rule_label: str
    verdict: Verdict
    description: str


class PositionResponse(PositionBase):
    """Schema for position response, including computed fields."""
    id: int
    percent_gain: float
    hold_duration_days: int
    verdict: Verdict
    triggered_rules: list[RuleResult]

    model_config = {"from_attributes": True}
