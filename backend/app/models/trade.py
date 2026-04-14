import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Enum, ForeignKey, JSON, Float, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class TradingMode(str, enum.Enum):
    PAPER = "paper"
    LIVE = "live"


class TradeStatus(str, enum.Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"
    ERROR = "error"


class TradeDirection(str, enum.Enum):
    LONG = "long"
    SHORT = "short"


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    broker_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("broker_accounts.id"), nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("trade_sessions.id"), nullable=True)

    mode: Mapped[TradingMode] = mapped_column(Enum(TradingMode), nullable=False)
    status: Mapped[TradeStatus] = mapped_column(Enum(TradeStatus), default=TradeStatus.PENDING)

    instrument: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[TradeDirection] = mapped_column(Enum(TradeDirection), nullable=False)
    contracts: Mapped[int] = mapped_column(Integer, default=1)

    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)

    entry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Broker-side order IDs
    broker_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    broker_sl_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    broker_tp_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    commission: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)

    exit_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    strategy: Mapped["Strategy"] = relationship("Strategy", back_populates="trades")
    broker_account: Mapped["BrokerAccount | None"] = relationship("BrokerAccount", back_populates="trades")


class TradeSession(Base):
    """Groups trades for a paper or live trading session."""
    __tablename__ = "trade_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    broker_account_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("broker_accounts.id"), nullable=True)

    mode: Mapped[TradingMode] = mapped_column(Enum(TradingMode), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Daily risk controls
    daily_loss_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_trades_today: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kill_switch_triggered: Mapped[bool] = mapped_column(Boolean, default=False)

    # Cumulative session stats
    total_trades: Mapped[int] = mapped_column(Integer, default=0)
    net_pnl: Mapped[float] = mapped_column(Float, default=0.0)
