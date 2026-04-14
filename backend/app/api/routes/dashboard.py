from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models.user import User
from app.models.trade import Trade, TradingMode, TradeStatus
from app.models.strategy import Strategy
from app.models.backtest import BacktestRun
from app.core.auth import get_current_user

router = APIRouter()


@router.get("/summary")
async def get_dashboard_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Strategy count
    strat_count = (await db.execute(
        select(func.count()).where(Strategy.user_id == current_user.id)
    )).scalar()

    # Backtest count
    bt_count = (await db.execute(
        select(func.count()).where(BacktestRun.user_id == current_user.id)
    )).scalar()

    # Paper trading stats
    paper_trades = (await db.execute(
        select(Trade).where(
            Trade.user_id == current_user.id,
            Trade.mode == TradingMode.PAPER,
            Trade.status == TradeStatus.CLOSED,
        )
    )).scalars().all()

    paper_pnl   = sum(t.net_pnl or 0 for t in paper_trades)
    paper_wins  = sum(1 for t in paper_trades if (t.net_pnl or 0) > 0)
    paper_wr    = (paper_wins / len(paper_trades)) if paper_trades else 0.0

    # Live trading stats
    live_trades = (await db.execute(
        select(Trade).where(
            Trade.user_id == current_user.id,
            Trade.mode == TradingMode.LIVE,
            Trade.status == TradeStatus.CLOSED,
        )
    )).scalars().all()

    live_pnl  = sum(t.net_pnl or 0 for t in live_trades)
    live_wins = sum(1 for t in live_trades if (t.net_pnl or 0) > 0)
    live_wr   = (live_wins / len(live_trades)) if live_trades else 0.0

    return {
        "strategy_count": strat_count,
        "backtest_count":  bt_count,
        "subscription_tier": current_user.subscription_tier.value,
        "paper_trading": {
            "total_trades": len(paper_trades),
            "net_pnl":      round(paper_pnl, 2),
            "win_rate":     round(paper_wr, 4),
        },
        "live_trading": {
            "total_trades": len(live_trades),
            "net_pnl":      round(live_pnl, 2),
            "win_rate":     round(live_wr, 4),
        },
    }
