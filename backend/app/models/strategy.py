import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, Enum, ForeignKey, JSON, Text, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class StrategyStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class ConditionType(str, enum.Enum):
    LIQUIDITY_SWEEP = "liquidity_sweep"
    FAIR_VALUE_GAP = "fair_value_gap"
    INVERSE_FVG = "inverse_fvg"
    SESSION_FILTER = "session_filter"
    TIMEFRAME_DROP = "timeframe_drop"
    PRICE_RETURN = "price_return"
    CUSTOM_INDICATOR = "custom_indicator"


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[StrategyStatus] = mapped_column(Enum(StrategyStatus, name="strategystatus"), default=StrategyStatus.DRAFT)

    # Instrument config
    instruments: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["ES", "NQ"]

    # Timeframe config
    primary_timeframe: Mapped[str] = mapped_column(String(10), default="15m")
    execution_timeframe: Mapped[str] = mapped_column(String(10), default="1m")
    higher_timeframes: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["1H", "4H"]

    # Risk management
    risk_reward_ratio: Mapped[float] = mapped_column(default=2.0)
    stop_loss_type: Mapped[str] = mapped_column(String(20), default="structure")  # "ticks" or "structure"
    stop_loss_ticks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Break-even management: once price runs this many R in our favour, the
    # stop slides to entry. 0.0 = off. This is part of the strategy DEFINITION
    # so the backtest, optimizer and live paths all model the SAME management
    # the trader actually uses — the single biggest driver of win rate.
    # (No explicit Float type: SQLAlchemy infers it from Mapped[float], matching
    # risk_reward_ratio above and avoiding an import assumption.)
    breakeven_at_r: Mapped[float | None] = mapped_column(nullable=True, default=0.0)
    # "off" | "r" (fixed R multiple) | "structure" (move to break-even when a
    # prior swing breaks — the way these setups are actually managed).
    breakeven_mode: Mapped[str | None] = mapped_column(String(16), nullable=True, default="off")
    max_contracts: Mapped[int] = mapped_column(Integer, default=1)

    # Session filter
    session_filters: Mapped[list] = mapped_column(JSON, default=list)  # e.g. ["NY", "LONDON"]

    # FVG config
    fvg_min_size_ticks: Mapped[int] = mapped_column(Integer, default=4)
    fvg_max_size_ticks: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Full strategy definition as structured JSON (rule tree)
    rule_tree: Mapped[dict] = mapped_column(JSON, default=dict)
    starred: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Risk controls
    max_daily_loss: Mapped[float | None] = mapped_column(nullable=True)
    max_trades_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kill_switch_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Overtrade-prevention knobs (default 5 min, 1 position). The runtime
    # ALTER TABLE in app.engines.entry_guard.ensure_strategy_columns adds
    # these to the live DB; this declaration keeps the ORM in sync so
    # writes via the Strategy model work.
    cooldown_min: Mapped[int | None] = mapped_column(Integer, nullable=True, default=5)
    max_open_positions: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="strategies")
    conditions: Mapped[list["StrategyCondition"]] = relationship("StrategyCondition", back_populates="strategy", cascade="all, delete-orphan")
    backtest_runs: Mapped[list["BacktestRun"]] = relationship("BacktestRun", back_populates="strategy")
    optimization_runs: Mapped[list["OptimizationRun"]] = relationship("OptimizationRun", back_populates="strategy")
    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="strategy")


class StrategyCondition(Base):
    """Individual conditions in a strategy's rule chain."""
    __tablename__ = "strategy_conditions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False)
    condition_type: Mapped[ConditionType] = mapped_column(Enum(ConditionType, name="conditiontype"), nullable=False)
    sequence_order: Mapped[int] = mapped_column(Integer, nullable=False)  # Order of execution
    timeframe: Mapped[str | None] = mapped_column(String(10), nullable=True)
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)

    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="conditions")
