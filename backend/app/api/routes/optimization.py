from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.models.strategy import Strategy
from app.models.optimization import OptimizationRun, OptimizationStatus, OptimizationResult
from app.core.auth import get_current_user, require_tier

router = APIRouter()

live_tiers = [SubscriptionTier.TIER_3, SubscriptionTier.TIER_4, SubscriptionTier.TIER_5]


class OptimizationRequest(BaseModel):
    strategy_id: str
    instrument: str = "ES"
    start_date: datetime
    end_date: datetime
    parameter_grid: dict
    optimization_metric: str = "profit_factor"


class OptimizationRunResponse(BaseModel):
    id: str
    strategy_id: str
    instrument: str
    status: str
    total_combinations: int
    completed_combinations: int
    created_at: str


class OptimizationResultResponse(BaseModel):
    rank: int
    parameters: dict
    net_profit: float
    profit_factor: float
    win_rate: float
    max_drawdown: float
    total_trades: int
    sharpe_ratio: Optional[float]


@router.post("/", response_model=OptimizationRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_optimization(
    data: OptimizationRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_tier(*live_tiers)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    from itertools import product
    combos = list(product(*data.parameter_grid.values()))
    total  = len(combos)

    run = OptimizationRun(
        strategy_id=strategy.id,
        user_id=current_user.id,
        instrument=data.instrument,
        start_date=data.start_date,
        end_date=data.end_date,
        parameter_grid=data.parameter_grid,
        optimization_metric=data.optimization_metric,
        total_combinations=total,
        status=OptimizationStatus.QUEUED,
    )
    db.add(run)
    await db.flush()

    background_tasks.add_task(_run_optimization_task, str(run.id))

    return OptimizationRunResponse(
        id=str(run.id), strategy_id=str(run.strategy_id), instrument=run.instrument,
        status=run.status.value, total_combinations=run.total_combinations,
        completed_combinations=run.completed_combinations, created_at=run.created_at.isoformat(),
    )


@router.get("/{run_id}/results", response_model=list[OptimizationResultResponse])
async def get_optimization_results(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OptimizationRun).where(
            OptimizationRun.id == run_id, OptimizationRun.user_id == current_user.id
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Optimization run not found.")

    results = await db.execute(
        select(OptimizationResult)
        .where(OptimizationResult.optimization_run_id == run.id)
        .order_by(OptimizationResult.rank)
    )
    return [
        OptimizationResultResponse(
            rank=r.rank, parameters=r.parameters, net_profit=r.net_profit,
            profit_factor=r.profit_factor, win_rate=r.win_rate, max_drawdown=r.max_drawdown,
            total_trades=r.total_trades, sharpe_ratio=r.sharpe_ratio,
        )
        for r in results.scalars().all()
    ]


async def _run_optimization_task(run_id: str):
    pass  # Celery task dispatch placeholder
