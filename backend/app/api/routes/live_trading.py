from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.models.strategy import Strategy
from app.models.user import BrokerAccount
from app.models.trade import TradeSession, TradingMode
from app.core.auth import get_current_user, require_live_trading
from app.core.security import encrypt_credentials, decrypt_credentials

router = APIRouter()


class AddBrokerAccountRequest(BaseModel):
    account_name: str
    broker: str = "tradovate"
    is_demo: bool = True
    credentials: dict  # {"username": ..., "password": ..., "app_id": ..., "cid": ..., "sec": ...}


class BrokerAccountResponse(BaseModel):
    id: str
    account_name: str
    broker: str
    is_demo: bool
    is_active: bool
    created_at: str


class StartLiveSessionRequest(BaseModel):
    strategy_id: str
    broker_account_id: str
    instrument: str = "ES"
    daily_loss_limit: Optional[float] = None
    max_trades_today: Optional[int] = None


# ─────────────────────────────────────────────────────────────────────────────
# Broker Accounts
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/accounts", response_model=BrokerAccountResponse, status_code=status.HTTP_201_CREATED)
async def add_broker_account(
    data: AddBrokerAccountRequest,
    current_user: User = Depends(require_live_trading),
    db: AsyncSession = Depends(get_db),
):
    # Enforce max account limits based on tier
    existing = await db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == current_user.id, BrokerAccount.is_active == True)
    )
    account_count = len(existing.scalars().all())

    limits = {
        SubscriptionTier.TIER_3: 5,
        SubscriptionTier.TIER_4: 20,
        SubscriptionTier.TIER_5: 999_999,
    }
    max_accounts = limits.get(current_user.subscription_tier, 0)
    if account_count >= max_accounts:
        raise HTTPException(
            status_code=403,
            detail=f"Your tier allows a maximum of {max_accounts} broker accounts.",
        )

    account = BrokerAccount(
        user_id=current_user.id,
        broker=data.broker,
        account_name=data.account_name,
        encrypted_credentials=encrypt_credentials(data.credentials),
        is_demo=data.is_demo,
    )
    db.add(account)
    await db.flush()

    return BrokerAccountResponse(
        id=str(account.id), account_name=account.account_name, broker=account.broker,
        is_demo=account.is_demo, is_active=account.is_active, created_at=account.created_at.isoformat(),
    )


@router.get("/accounts", response_model=list[BrokerAccountResponse])
async def list_broker_accounts(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BrokerAccount).where(BrokerAccount.user_id == current_user.id)
    )
    return [
        BrokerAccountResponse(
            id=str(a.id), account_name=a.account_name, broker=a.broker,
            is_demo=a.is_demo, is_active=a.is_active, created_at=a.created_at.isoformat(),
        )
        for a in result.scalars().all()
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Live Sessions
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def start_live_session(
    data: StartLiveSessionRequest,
    current_user: User = Depends(require_live_trading),
    db: AsyncSession = Depends(get_db),
):
    # Validate strategy + account ownership
    strat = (await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )).scalar_one_or_none()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    acct = (await db.execute(
        select(BrokerAccount).where(BrokerAccount.id == data.broker_account_id, BrokerAccount.user_id == current_user.id)
    )).scalar_one_or_none()
    if not acct:
        raise HTTPException(status_code=404, detail="Broker account not found.")

    session = TradeSession(
        strategy_id=strat.id,
        user_id=current_user.id,
        broker_account_id=acct.id,
        mode=TradingMode.LIVE,
        is_active=True,
        daily_loss_limit=data.daily_loss_limit,
        max_trades_today=data.max_trades_today,
    )
    db.add(session)
    await db.flush()

    # In production: dispatch live trader to Celery worker here
    # celery_task = start_live_trader.delay(str(session.id))
    # session.celery_task_id = celery_task.id

    return {"session_id": str(session.id), "status": "started"}


@router.post("/sessions/{session_id}/kill-switch")
async def trigger_kill_switch(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TradeSession).where(TradeSession.id == session_id, TradeSession.user_id == current_user.id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    session.kill_switch_triggered = True
    session.is_active = False

    # In production: send kill signal to Celery worker
    # revoke_live_trader.delay(session_id)

    return {"status": "kill_switch_triggered", "session_id": session_id}
