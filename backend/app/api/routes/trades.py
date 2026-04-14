from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models.user import User
from app.models.trade import Trade, TradingMode
from app.core.auth import get_current_user

router = APIRouter()


class TradeResponse(BaseModel):
    id: str
    strategy_id: str
    instrument: str
    direction: str
    mode: str
    status: str
    entry_price: Optional[float]
    exit_price: Optional[float]
    stop_loss: float
    take_profit: float
    contracts: int
    pnl: Optional[float]
    net_pnl: Optional[float]
    entry_time: Optional[str]
    exit_time: Optional[str]
    exit_reason: Optional[str]


@router.get("/", response_model=list[TradeResponse])
async def list_trades(
    mode: Optional[str] = None,
    strategy_id: Optional[str] = None,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    query = select(Trade).where(Trade.user_id == current_user.id)
    if mode:
        query = query.where(Trade.mode == mode)
    if strategy_id:
        query = query.where(Trade.strategy_id == strategy_id)
    query = query.order_by(Trade.created_at.desc()).limit(limit)

    result = await db.execute(query)
    return [
        TradeResponse(
            id=str(t.id), strategy_id=str(t.strategy_id), instrument=t.instrument,
            direction=t.direction.value, mode=t.mode.value, status=t.status.value,
            entry_price=t.entry_price, exit_price=t.exit_price,
            stop_loss=t.stop_loss, take_profit=t.take_profit, contracts=t.contracts,
            pnl=t.pnl, net_pnl=t.net_pnl,
            entry_time=t.entry_time.isoformat() if t.entry_time else None,
            exit_time=t.exit_time.isoformat() if t.exit_time else None,
            exit_reason=t.exit_reason,
        )
        for t in result.scalars().all()
    ]
