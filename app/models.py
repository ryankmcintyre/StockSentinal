from datetime import date, datetime

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    """A user authenticated via Supabase Auth.

    The ``id`` matches the Supabase Auth user UUID so no separate mapping is needed.
    """
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    email = Column(String, nullable=True)
    display_name = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    positions = relationship("Position", back_populates="user", cascade="all, delete-orphan")
    strategy_rule_configs = relationship(
        "StrategyRuleConfig", back_populates="user", cascade="all, delete-orphan"
    )


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

    # --- Cached market data from the configured provider ---
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
    refresh_in_progress = Column(Boolean, nullable=True, default=False)
    refresh_started_at = Column(DateTime, nullable=True)

    # Per-position sector benchmark ticker (issue #22). Optional;
    # the relative-weakness rule is skipped when missing.
    sector_benchmark_ticker = Column(String, nullable=True)

    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    user = relationship("User", back_populates="positions")

    # Manually identified historical key levels (issue #23 — failed
    # breakout / reclaim failure rule).  Cascade on delete so removing
    # a position cleans up its key levels.
    key_levels = relationship(
        "PositionKeyLevel",
        back_populates="position",
        cascade="all, delete-orphan",
        order_by="PositionKeyLevel.level_price",
    )


class PositionKeyLevel(Base):
    """A manually identified key resistance/support level for a position.

    Used by the SELL_FAILED_BREAKOUT_RECLAIM rule (issue #23).
    """
    __tablename__ = "position_key_levels"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(Integer, ForeignKey("positions.id", ondelete="CASCADE"), nullable=False)
    level_price = Column(Float, nullable=False)
    label = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    position = relationship("Position", back_populates="key_levels")


class MarketIndicatorCache(Base):
    """Cache for arbitrary SMA indicators fetched from the market data provider.

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
    """Cache for ATR (Average True Range) indicator values fetched from the market data provider.

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


class MarketWeeklyBarCache(Base):
    """Cache for weekly OHLCV bars per ticker.

    Used by rules that need historical weekly price action (issue #19
    upper-wick detection, plus future #20 volume distribution and #21
    pivot detection).
    """
    __tablename__ = "market_weekly_bar_cache"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "bar_date",
            name="uq_weekly_bar_ticker_date",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, nullable=False)
    bar_date = Column(Date, nullable=False)
    open = Column(Float, nullable=True)
    high = Column(Float, nullable=True)
    low = Column(Float, nullable=True)
    close = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    retrieved_at = Column(DateTime, nullable=True)


class MarketDailyBarCache(Base):
    """Cache for daily close prices per ticker.

    Used by rules that need historical daily price action over a
    rolling window (issue #22 relative-weakness comparison vs sector
    benchmark).  Stores enough trailing closes to compute the largest
    configured lookback return.
    """
    __tablename__ = "market_daily_bar_cache"
    __table_args__ = (
        UniqueConstraint(
            "ticker", "bar_date",
            name="uq_daily_bar_ticker_date",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String, nullable=False)
    bar_date = Column(Date, nullable=False)
    close = Column(Float, nullable=True)
    retrieved_at = Column(DateTime, nullable=True)


class StrategyRuleConfig(Base):
    __tablename__ = "strategy_rule_configs"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "investment_type", "rule_key",
            name="uq_strategy_rule_configs_user_type_key",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    investment_type = Column(String, nullable=False)  # "long-term" or "short-term"
    rule_key = Column(String, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    params_json = Column(String, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    user = relationship("User", back_populates="strategy_rule_configs")
