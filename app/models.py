from datetime import date, datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, UniqueConstraint
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


class MarketIndicatorCache(Base):
    """Cache for arbitrary SMA indicators fetched from Alpha Vantage.

    Each row stores a close price and SMA value for a specific
    (ticker, interval, time_period) combination.  The close and SMA
    dates should align to the same completed bar so comparisons are
    valid.
    """
    __tablename__ = "market_indicator_cache"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "interval", "time_period",
            name="uq_mic_ticker_interval_period",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, nullable=False)
    interval = Column(String, nullable=False)  # "daily" or "weekly"
    time_period = Column(Integer, nullable=False)
    sma_value = Column(Float, nullable=True)
    sma_date = Column(Date, nullable=True)
    close_value = Column(Float, nullable=True)
    close_date = Column(Date, nullable=True)
    retrieved_at = Column(DateTime, nullable=True)


class MarketAtrCache(Base):
    """Cache for ATR (Average True Range) indicator values fetched from Alpha Vantage.

    Each row stores the latest ATR value for a specific
    (ticker, interval, time_period) combination.  Used by the extension
    rules (TRIM_EXTENSION_ATR / SELL_EXTENSION_ATR) which compute how many
    multiples of ATR a price sits above its moving average.
    """
    __tablename__ = "market_atr_cache"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "interval", "time_period",
            name="uq_atr_ticker_interval_period",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, nullable=False)
    interval = Column(String, nullable=False)  # "daily" or "weekly"
    time_period = Column(Integer, nullable=False)
    atr_value = Column(Float, nullable=True)
    atr_date = Column(Date, nullable=True)
    retrieved_at = Column(DateTime, nullable=True)


class StrategyRuleConfig(Base):
    __tablename__ = "strategy_rule_configs"
    __table_args__ = (
        UniqueConstraint("investment_type", "rule_key", name="uq_strategy_rule_configs_type_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    investment_type = Column(String, nullable=False)  # "long-term" or "short-term"
    rule_key = Column(String, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    params_json = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
