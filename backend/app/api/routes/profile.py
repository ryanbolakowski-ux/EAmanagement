from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta

from app.database import get_db
from app.models.user import User, BrokerAccount
from app.models.trade import Trade, TradeSession
from app.core.auth import get_current_user

router = APIRouter()

MASTER_CODE = ""  # disabled — was a hardcoded promo bypass
FREETRIAL_CODE = "FREETRIAL"

TIER_INFO = {
    "free_trial": {"name": "Tier 1 (Free Trial)", "price": 0},
    "tier_2":     {"name": "Tier 2 (Futures Signals)",    "price": 49},
    "tier_3":     {"name": "Tier 3 (Options Scanner)",    "price": 99},
    "tier_4":     {"name": "Tier 4 (Options Live)",       "price": 199},
    "tier_5":     {"name": "Tier 5 (Fully Automated)",    "price": 399},
}


class ProfileResponse(BaseModel):
    id: str
    email: str
    username: str
    subscription_tier: str
    tier_name: str
    tier_price: float
    is_active: bool
    created_at: str
    lifetime_pnl: float
    total_trades: int
    win_rate: float
    accounts: list[dict]
    active_paper_sessions: int


class ApplyCodeRequest(BaseModel):
    code: str


class UpgradeTierRequest(BaseModel):
    tier: str
    promo_code: Optional[str] = None


@router.get("/me", response_model=ProfileResponse)
async def get_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Lifetime P&L from all trades
    result = await db.execute(
        select(func.coalesce(func.sum(Trade.net_pnl), 0.0), func.count(Trade.id))
        .where(Trade.user_id == current_user.id)
    )
    row = result.one()
    lifetime_pnl = float(row[0])
    total_trades = int(row[1])

    # Win rate
    if total_trades > 0:
        wins_result = await db.execute(
            select(func.count(Trade.id)).where(Trade.user_id == current_user.id, Trade.net_pnl > 0)
        )
        wins = wins_result.scalar() or 0
        win_rate = wins / total_trades
    else:
        win_rate = 0.0

    # Broker accounts
    accts_result = await db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == current_user.id)
    )
    accounts = [{"id": str(a.id), "broker": a.broker, "name": a.account_name, "is_demo": a.is_demo, "is_active": a.is_active} for a in accts_result.scalars().all()]

    # Active paper sessions
    paper_result = await db.execute(
        select(func.count(TradeSession.id)).where(TradeSession.user_id == current_user.id, TradeSession.is_active == True)
    )
    active_paper = paper_result.scalar() or 0

    tier = current_user.subscription_tier or "free_trial"
    info = TIER_INFO.get(tier, TIER_INFO["free_trial"])

    return ProfileResponse(
        id=str(current_user.id),
        email=current_user.email,
        username=current_user.username,
        subscription_tier=tier,
        tier_name=info["name"],
        tier_price=info["price"],
        is_active=current_user.is_active,
        created_at=current_user.created_at.isoformat() if current_user.created_at else "",
        lifetime_pnl=lifetime_pnl,
        total_trades=total_trades,
        win_rate=win_rate,
        accounts=accounts,
        active_paper_sessions=active_paper,
    )


@router.post("/upgrade")
async def upgrade_tier(
    data: UpgradeTierRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if data.promo_code == FREETRIAL_CODE:
        current_user.subscription_tier = "free_trial"
        current_user.trial_ends_at = datetime.utcnow() + timedelta(days=30)
        await db.commit()
        return {"status": "upgraded", "tier": "free_trial", "tier_name": "Free Trial (30 days)", "paid": 0}

    valid_tiers = ["tier_2", "tier_3", "tier_4", "tier_5"]
    if data.tier not in valid_tiers:
        raise HTTPException(status_code=400, detail="Invalid tier")

    if data.promo_code and MASTER_CODE and data.promo_code == MASTER_CODE:
        current_user.subscription_tier = data.tier
        await db.commit()
        info = TIER_INFO.get(data.tier, {})
        return {"status": "upgraded", "tier": data.tier, "tier_name": info.get("name", ""), "paid": 0}

    # TODO: Stripe integration for paid upgrades
    raise HTTPException(status_code=400, detail="Payment required. Stripe integration coming soon.")
