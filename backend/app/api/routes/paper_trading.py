from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.models.strategy import Strategy
from app.models.trade import TradeSession, TradingMode
from app.core.auth import get_current_user, require_tier

router = APIRouter()

eligible_tiers = [SubscriptionTier.FREE_TRIAL, SubscriptionTier.TIER_3, SubscriptionTier.TIER_4, SubscriptionTier.TIER_5]


class StartPaperSessionRequest(BaseModel):
    strategy_id: str
    instrument: str = "ES"
    daily_loss_limit: Optional[float] = None
    max_trades_today: Optional[int] = None


class SessionResponse(BaseModel):
    id: str
    strategy_id: str
    mode: str
    is_active: bool
    started_at: str
    total_trades: int
    net_pnl: float


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def start_paper_session(
    data: StartPaperSessionRequest,
    current_user: User = Depends(require_tier(*eligible_tiers)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # Stop any existing active paper sessions for this strategy
    existing = await db.execute(
        select(TradeSession).where(
            TradeSession.user_id == current_user.id,
            TradeSession.strategy_id == strategy.id,
            TradeSession.mode == TradingMode.PAPER,
            TradeSession.is_active == True,
        )
    )
    for sess in existing.scalars().all():
        sess.is_active = False

    session = TradeSession(
        strategy_id=strategy.id,
        user_id=current_user.id,
        mode=TradingMode.PAPER,
        is_active=True,
        daily_loss_limit=data.daily_loss_limit,
        max_trades_today=data.max_trades_today,
    )
    db.add(session)
    await db.flush()

    return SessionResponse(
        id=str(session.id), strategy_id=str(session.strategy_id),
        mode=session.mode.value, is_active=session.is_active,
        started_at=session.started_at.isoformat(),
        total_trades=session.total_trades, net_pnl=session.net_pnl,
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def list_paper_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TradeSession).where(
            TradeSession.user_id == current_user.id,
            TradeSession.mode == TradingMode.PAPER,
        ).order_by(TradeSession.started_at.desc())
    )
    return [
        SessionResponse(
            id=str(s.id), strategy_id=str(s.strategy_id), mode=s.mode.value,
            is_active=s.is_active, started_at=s.started_at.isoformat(),
            total_trades=s.total_trades, net_pnl=s.net_pnl,
        )
        for s in result.scalars().all()
    ]


@router.post("/sessions/{session_id}/stop")
async def stop_paper_session(
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
    session.is_active = False
    from datetime import datetime
    session.ended_at = datetime.utcnow()
    return {"status": "stopped"}
