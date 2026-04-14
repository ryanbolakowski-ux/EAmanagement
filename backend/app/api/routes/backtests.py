from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from app.database import get_db
from app.models.user import User
from app.models.strategy import Strategy
from app.models.backtest import BacktestRun, BacktestStatus, BacktestMetrics, BacktestTrade
from app.core.auth import get_current_user

router = APIRouter()


class BacktestRequest(BaseModel):
    strategy_id: str
    instrument: str = "ES"
    start_date: datetime
    end_date: datetime
    timeframe: str = "15m"
    initial_capital: float = 100_000.0
    commission_per_side: float = 2.25
    slippage_ticks: int = 1


class BacktestRunResponse(BaseModel):
    id: str
    strategy_id: str
    instrument: str
    start_date: str
    end_date: str
    status: str
    created_at: str
    completed_at: Optional[str] = None


class MetricsResponse(BaseModel):
    total_trades: int
    win_rate: float
    net_profit: float
    profit_factor: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: Optional[float]
    avg_rr: float
    equity_curve: list
    monthly_returns: dict


@router.post("/", response_model=BacktestRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_backtest(
    data: BacktestRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Validate strategy ownership
    result = await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    run = BacktestRun(
        strategy_id=strategy.id,
        user_id=current_user.id,
        instrument=data.instrument,
        start_date=data.start_date,
        end_date=data.end_date,
        timeframe=data.timeframe,
        initial_capital=data.initial_capital,
        commission_per_side=data.commission_per_side,
        slippage_ticks=data.slippage_ticks,
        strategy_params_snapshot={
            "primary_timeframe": strategy.primary_timeframe,
            "execution_timeframe": strategy.execution_timeframe,
            "risk_reward_ratio": strategy.risk_reward_ratio,
            "stop_loss_ticks": strategy.stop_loss_ticks,
            "fvg_min_size_ticks": strategy.fvg_min_size_ticks,
            "session_filters": strategy.session_filters,
        },
        status=BacktestStatus.QUEUED,
    )
    db.add(run)
    await db.flush()

    # Queue backtest task via Celery
    background_tasks.add_task(_run_backtest_task, str(run.id))

    return BacktestRunResponse(
        id=str(run.id),
        strategy_id=str(run.strategy_id),
        instrument=run.instrument,
        start_date=run.start_date.isoformat(),
        end_date=run.end_date.isoformat(),
        status=run.status.value,
        created_at=run.created_at.isoformat(),
    )


@router.get("/", response_model=list[BacktestRunResponse])
async def list_backtests(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BacktestRun).where(BacktestRun.user_id == current_user.id).order_by(BacktestRun.created_at.desc())
    )
    return [
        BacktestRunResponse(
            id=str(r.id), strategy_id=str(r.strategy_id), instrument=r.instrument,
            start_date=r.start_date.isoformat(), end_date=r.end_date.isoformat(),
            status=r.status.value, created_at=r.created_at.isoformat(),
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
        )
        for r in result.scalars().all()
    ]


@router.get("/{backtest_id}/metrics", response_model=MetricsResponse)
async def get_backtest_metrics(
    backtest_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BacktestRun).where(BacktestRun.id == backtest_id, BacktestRun.user_id == current_user.id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found.")
    if run.status != BacktestStatus.COMPLETED:
        raise HTTPException(status_code=400, detail=f"Backtest is {run.status.value}, not completed.")

    m_result = await db.execute(select(BacktestMetrics).where(BacktestMetrics.backtest_run_id == run.id))
    m = m_result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Metrics not yet calculated.")

    return MetricsResponse(
        total_trades=m.total_trades, win_rate=m.win_rate, net_profit=m.net_profit,
        profit_factor=m.profit_factor, max_drawdown=m.max_drawdown, max_drawdown_pct=m.max_drawdown_pct,
        sharpe_ratio=m.sharpe_ratio, avg_rr=m.avg_rr,
        equity_curve=m.equity_curve, monthly_returns=m.monthly_returns,
    )


async def _run_backtest_task(backtest_run_id: str):
    """Placeholder: dispatches to Celery worker in production."""
    # from app.tasks.backtest_task import run_backtest_celery
    # run_backtest_celery.delay(backtest_run_id)
    pass
