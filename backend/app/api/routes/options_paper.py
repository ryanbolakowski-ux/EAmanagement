"""API routes for options paper trading."""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from typing import Optional

from app.database import get_db, async_session_factory
from app.core.auth import require_2fa_when_paid as get_current_user, require_tier
from app.models.user import User, SubscriptionTier
from app.models.strategy import Strategy
from app.models.trade import TradeSession, TradingMode


router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription

eligible_tiers = (
    SubscriptionTier.FREE_TRIAL, SubscriptionTier.TIER_2, SubscriptionTier.TIER_3,
    SubscriptionTier.TIER_4, SubscriptionTier.TIER_5,
)

OPT_TICKERS = {"SPY","QQQ","NVDA","AAPL","MSFT","TSLA","AMD","META","AMZN","GOOGL","JPM","KO"}


class StartOptionsPaperRequest(BaseModel):
    strategy_id: str
    underlying: Optional[str] = None              # legacy single-underlying
    watchlist: Optional[list[str]] = None         # NEW: scan many tickers, pick best
    daily_loss_limit: Optional[float] = None


class OptionsPaperSessionResponse(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    underlying: str
    is_active: bool
    started_at: Optional[str]
    total_trades: int
    net_pnl: Optional[float]


@router.post("/sessions", response_model=OptionsPaperSessionResponse, status_code=status.HTTP_201_CREATED)
async def start_options_paper(
    data: StartOptionsPaperRequest,
    current_user: User = Depends(require_tier(*eligible_tiers)),
    db: AsyncSession = Depends(get_db),
):
    """Start an options paper-trading session. The strategy must be an
    options strategy (options_mode set, or trades on options tickers).
    Underlying is the ticker to trade options against."""
    strat = (await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )).scalar_one_or_none()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    is_options = bool(getattr(strat, "options_mode", None)) or bool(
        set(strat.instruments or []) & OPT_TICKERS
    )
    if not is_options:
        raise HTTPException(
            status_code=400,
            detail="Options paper trading requires an options strategy. Pick a strategy with an options_mode set, or use the regular futures paper trading.",
        )

    # Multi-underlying watchlist by default. Use strategy's instruments,
    # then default to a curated top-movers universe.
    DEFAULT_WATCH = [
        "SPY","QQQ","IWM","DIA",                                # ETFs
        "NVDA","AAPL","MSFT","TSLA","AMD","META","AMZN","GOOGL", # mega-caps
        "JPM","BAC","KO","DIS","NFLX","COIN","PLTR","UBER",      # liquid singles
    ]
    watch = (data.watchlist
             or (strat.instruments if strat.instruments and len(strat.instruments) > 0 else None)
             or DEFAULT_WATCH)
    underlying = (data.underlying or watch[0]).upper()

    # Check for an existing active session on the same (strategy, underlying)
    existing = await db.execute(
        select(TradeSession).where(
            TradeSession.user_id == current_user.id,
            TradeSession.strategy_id == strat.id,
            TradeSession.mode == "options_paper",
            TradeSession.is_active == True,
            TradeSession.instrument == underlying,
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail=f"An options paper session for {strat.name} on {underlying} is already running. Stop it first.",
        )

    sess = TradeSession(
        strategy_id=strat.id,
        user_id=current_user.id,
        mode="options_paper",
        is_active=True,
        instrument=underlying,
        daily_loss_limit=data.daily_loss_limit,
    )
    db.add(sess)
    await db.commit()
    await db.refresh(sess)

    # Spawn the runner
    try:
        from app.engines.options.options_paper_runner import start_options_paper_session as _start
        import asyncio
        # Pass the full watchlist so the runner can rotate through it
        asyncio.create_task(_start(str(sess.id), str(strat.id), str(current_user.id), underlying, watchlist=watch))
    except Exception as e:
        from loguru import logger
        logger.error(f"[options_paper] runner dispatch failed: {e}")

    return OptionsPaperSessionResponse(
        id=str(sess.id), strategy_id=str(strat.id), strategy_name=strat.name,
        underlying=underlying, is_active=True,
        started_at=sess.started_at.isoformat() if sess.started_at else None,
        total_trades=0, net_pnl=0.0,
    )


@router.get("/sessions", response_model=list[OptionsPaperSessionResponse])
async def list_options_paper_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = await db.execute(text("""
        SELECT ts.id, ts.strategy_id, s.name, ts.instrument, ts.is_active,
               ts.started_at, ts.total_trades, ts.net_pnl
          FROM trade_sessions ts
          JOIN strategies s ON s.id = ts.strategy_id
         WHERE ts.user_id = :uid AND ts.mode = 'options_paper'
         ORDER BY ts.started_at DESC NULLS LAST
    """), {"uid": str(current_user.id)})
    return [
        OptionsPaperSessionResponse(
            id=str(r[0]), strategy_id=str(r[1]), strategy_name=r[2] or "",
            underlying=r[3] or "", is_active=bool(r[4]),
            started_at=r[5].isoformat() if r[5] else None,
            total_trades=int(r[6] or 0), net_pnl=float(r[7] or 0),
        )
        for r in rows.fetchall()
    ]


@router.post("/sessions/{session_id}/stop")
async def stop_options_paper(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sess = (await db.execute(
        select(TradeSession).where(
            TradeSession.id == session_id,
            TradeSession.user_id == current_user.id,
            TradeSession.mode == "options_paper",
        )
    )).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found.")
    sess.is_active = False
    await db.commit()
    try:
        from app.engines.options.options_paper_runner import stop_options_paper_session as _stop
        await _stop(session_id)
    except Exception:
        pass
    return {"status": "stopped"}
