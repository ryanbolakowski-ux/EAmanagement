import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Enum, ForeignKey, JSON, Float, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class OptimizationStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class OptimizationRun(Base):
    __tablename__ = "optimization_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)

    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Parameter ranges to optimize
    parameter_grid: Mapped[dict] = mapped_column(JSON, nullable=False)
    # e.g. {
    #   "risk_reward_ratio": [1.5, 2.0, 2.5, 3.0],
    #   "stop_loss_ticks": [8, 10, 12, 16],
    #   "fvg_min_size_ticks": [2, 4, 6],
    #   "primary_timeframe": ["5m", "15m", "1H"]
    # }

    optimization_metric: Mapped[str] = mapped_column(String(50), default="profit_factor")
    # Options: "profit_factor", "net_profit", "win_rate", "sharpe_ratio"

    total_combinations: Mapped[int] = mapped_column(Integer, default=0)
    completed_combinations: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[OptimizationStatus] = mapped_column(Enum(OptimizationStatus), default=OptimizationStatus.QUEUED)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="optimization_runs")
    results: Mapped[list["OptimizationResult"]] = relationship("OptimizationResult", back_populates="optimization_run", cascade="all, delete-orphan")


class OptimizationResult(Base):
    __tablename__ = "optimization_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    optimization_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("optimization_runs.id"), nullable=False)

    parameters: Mapped[dict] = mapped_column(JSON, nullable=False)  # The specific combo tested
    rank: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 = best

    # Metrics from the backtest run for this combination
    net_profit: Mapped[float] = mapped_column(Float, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, default=0.0)
    max_drawdown: Mapped[float] = mapped_column(Float, default=0.0)
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    sharpe_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    backtest_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("backtest_runs.id"), nullable=True)

    optimization_run: Mapped["OptimizationRun"] = relationship("OptimizationRun", back_populates="results")
