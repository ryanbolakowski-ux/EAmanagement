import uuid
import enum
from datetime import datetime, date
from sqlalchemy import String, Boolean, DateTime, Enum, ForeignKey, Text, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID
from app.database import Base


class SubscriptionTier(str, enum.Enum):
    FREE_TRIAL = "free_trial"   # 30-day trial, scanner preview, paper only
    TIER_2 = "tier_2"            # $49 — Futures signals (Apex/TPT/Topstep)
    TIER_3 = "tier_3"            # $99 — Options scanner morning email
    TIER_4 = "tier_4"            # $199 — Options live via Tradier (manual confirm)
    TIER_5 = "tier_5"            # $399 — Fully automated, no clicks
    # Legacy alias for migrations; not exposed in UI
    TIER_1 = "tier_1"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_admin: Mapped[bool]  = mapped_column(Boolean, default=False, nullable=False)
    admin_passcode_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Comp (free-tier-granted-by-admin) tracking
    comp_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comp_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comp_granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    comp_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    subscription_tier: Mapped[str] = mapped_column(
        String(20), default="free_trial"
    )
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subscription_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subscription_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    # KYC fields (added for US-only compliance)
    kyc_status: Mapped[str | None] = mapped_column(String(20), default="not_started", nullable=True)
    kyc_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    kyc_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    kyc_session_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    country_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    strategies: Mapped[list["Strategy"]] = relationship("Strategy", back_populates="user", cascade="all, delete-orphan")
    broker_accounts: Mapped[list["BrokerAccount"]] = relationship("BrokerAccount", back_populates="user", cascade="all, delete-orphan")


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    broker: Mapped[str] = mapped_column(String(50), nullable=False, default="tradovate")
    account_name: Mapped[str] = mapped_column(String(100), nullable=False)
    encrypted_credentials: Mapped[str] = mapped_column(Text, nullable=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    # Sandbox mode: when True, the bot SIMULATES every order (logs it as if
    # it placed it) without actually routing to the broker. Default ON for
    # every new account so users can verify the bot behaves correctly before
    # risking real money. User must explicitly toggle OFF to go live.
    sandbox_mode: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    profit_target: Mapped[float | None] = mapped_column(Float, nullable=True)
    consistency_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    consistency_locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    account_id_at_broker: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Position sizing settings — the user-configured rules the bot uses to
    # decide how many shares/contracts to enter per trade.
    account_type: Mapped[str] = mapped_column(String(10), default="cash")          # "cash" | "margin"
    risk_per_trade_usd: Mapped[float | None] = mapped_column(Float, nullable=True)  # fixed $ risk per trade
    risk_per_trade_pct: Mapped[float | None] = mapped_column(Float, default=1.0)   # % of equity per trade (alt to fixed $)
    max_position_usd: Mapped[float | None] = mapped_column(Float, nullable=True)   # hard cap on capital deployed per trade

    # Last-fetched broker balance (refreshed when user opens sizing modal).
    cached_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    cached_buying_power: Mapped[float | None] = mapped_column(Float, nullable=True)
    cached_balance_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="broker_accounts")
    trades: Mapped[list["Trade"]] = relationship("Trade", back_populates="broker_account")
