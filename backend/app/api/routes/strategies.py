import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models.user import User
from app.models.strategy import Strategy, StrategyStatus
from app.core.auth import get_current_user

router = APIRouter()


class StrategyCreate(BaseModel):
    name: str
    description: Optional[str] = None
    instruments: list[str] = ["ES"]
    primary_timeframe: str = "15m"
    execution_timeframe: str = "1m"
    higher_timeframes: list[str] = []
    risk_reward_ratio: float = 2.0
    stop_loss_type: str = "structure"
    stop_loss_ticks: Optional[int] = None
    max_contracts: int = 1
    session_filters: list[str] = []
    fvg_min_size_ticks: int = 4
    fvg_max_size_ticks: Optional[int] = None
    max_daily_loss: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    rule_tree: dict = {}


class StrategyResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    status: str
    instruments: list
    primary_timeframe: str
    execution_timeframe: str
    risk_reward_ratio: float
    stop_loss_type: str
    session_filters: list
    created_at: str

    class Config:
        from_attributes = True


@router.get("/", response_model=list[StrategyResponse])
async def list_strategies(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.user_id == current_user.id)
    )
    return [
        StrategyResponse(
            id=str(s.id), name=s.name, description=s.description,
            status=s.status.value, instruments=s.instruments,
            primary_timeframe=s.primary_timeframe, execution_timeframe=s.execution_timeframe,
            risk_reward_ratio=s.risk_reward_ratio, stop_loss_type=s.stop_loss_type,
            session_filters=s.session_filters, created_at=s.created_at.isoformat(),
        )
        for s in result.scalars().all()
    ]


@router.post("/", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    data: StrategyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    strategy = Strategy(
        user_id=current_user.id,
        **data.model_dump(),
    )
    db.add(strategy)
    await db.flush()
    return StrategyResponse(
        id=str(strategy.id), name=strategy.name, description=strategy.description,
        status=strategy.status.value, instruments=strategy.instruments,
        primary_timeframe=strategy.primary_timeframe, execution_timeframe=strategy.execution_timeframe,
        risk_reward_ratio=strategy.risk_reward_ratio, stop_loss_type=strategy.stop_loss_type,
        session_filters=strategy.session_filters, created_at=strategy.created_at.isoformat(),
    )


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(
    strategy_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    return StrategyResponse(
        id=str(strategy.id), name=strategy.name, description=strategy.description,
        status=strategy.status.value, instruments=strategy.instruments,
        primary_timeframe=strategy.primary_timeframe, execution_timeframe=strategy.execution_timeframe,
        risk_reward_ratio=strategy.risk_reward_ratio, stop_loss_type=strategy.stop_loss_type,
        session_filters=strategy.session_filters, created_at=strategy.created_at.isoformat(),
    )


@router.put("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: str,
    data: StrategyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    for key, value in data.model_dump().items():
        setattr(strategy, key, value)
    await db.flush()
    return StrategyResponse(
        id=str(strategy.id), name=strategy.name, description=strategy.description,
        status=strategy.status.value, instruments=strategy.instruments,
        primary_timeframe=strategy.primary_timeframe, execution_timeframe=strategy.execution_timeframe,
        risk_reward_ratio=strategy.risk_reward_ratio, stop_loss_type=strategy.stop_loss_type,
        session_filters=strategy.session_filters, created_at=strategy.created_at.isoformat(),
    )


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strategy(
    strategy_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    await db.delete(strategy)
