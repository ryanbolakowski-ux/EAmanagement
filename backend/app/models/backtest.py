import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Enum, ForeignKey, JSON, Float, Integer, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class BacktestStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TradeDirection(str, enum.Enum):
    LONG = "long"
    SHORT = "short"


class BacktestRun(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    # Run config
    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    initial_capital: Mapped[float] = mapped_column(Float, default=100000.0)
    commission_per_side: Mapped[float] = mapped_column(Float, default=2.25)  # per contract
    slippage_ticks: Mapped[int] = mapped_column(Integer, default=1)

    # Snapshot of strategy params at time of run
    strategy_params_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)

    # Status
    status: Mapped[BacktestStatus] = mapped_column(Enum(BacktestStatus), default=BacktestStatus.QUEUED)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # Relationships
    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="backtest_runs")
    metrics: Mapped["BacktestMetrics | None"] = relationship("BacktestMetrics", back_populates="backtest_run", uselist=False)
    trades: Mapped[list["BacktestTrade"]] = relationship("BacktestTrade", back_populates="backtest_run", cascade="all, delete-orphan")


class BacktestMetrics(Base):
    __tablename__ = "backtest_metrics"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("backtest_runs.id"), nullable=False, unique=True)

    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    winning_trades: Mapped[int] = mapped_column(Integer, default=0)
    losing_trades: Mapped[int] = mapped_column(Integer, default=0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)

    net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    gross_profit: Mapped[float] = mapped_column(Float, default=0.0)
    gross_loss: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)

    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    sortino_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    avg_win: Mapped[float] = mapped_column(Float, default=0.0)
    avg_loss: Mapped[float] = mapped_column(Float, default=0.0)
    avg_rr: Mapped[float] = mapped_column(Float, default=0.0)
    largest_win: Mapped[float] = mapped_column(Float, default=0.0)
    largest_loss: Mapped[float] = mapped_column(Float, default=0.0)

    avg_trade_duration_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)  # [{timestamp, equity}, ...]
    monthly_returns: Mapped[dict] = mapped_column(JSON, default=dict)

    backtest_run: Mapped["BacktestRun"] = relationship("BacktestRun", back_populates="metrics")


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("backtest_runs.id"), nullable=False)

    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[TradeDirection] = mapped_column(Enum(TradeDirection), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    exit_price: Mapped[float] = mapped_column(Float, nullable=False)
    contracts: Mapped[int] = mapped_column(Integer, default=1)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)

    entry_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    exit_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    pnl: Mapped[float] = mapped_column(Float, nullable=False)
    pnl_ticks: Mapped[float] = mapped_column(Float, nullable=False)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float] = mapped_column(Float, nullable=False)

    is_winner: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(50), default="tp_hit")  # "tp_hit", "sl_hit", "manual"

    # Conditions met at entry (for analysis)
    conditions_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)

    backtest_run: Mapped["BacktestRun"] = relationship("BacktestRun", back_populates="trades")
