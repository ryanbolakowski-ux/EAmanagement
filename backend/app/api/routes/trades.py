from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models.user import User
from app.models.trade import Trade, TradingMode
from app.core.auth import require_2fa_when_paid as get_current_user

router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription


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
            direction=t.direction, mode=t.mode, status=t.status,
            entry_price=t.entry_price, exit_price=t.exit_price,
            stop_loss=t.stop_loss, take_profit=t.take_profit, contracts=t.contracts,
            pnl=t.pnl, net_pnl=t.net_pnl,
            entry_time=t.entry_time.isoformat() if t.entry_time else None,
            exit_time=t.exit_time.isoformat() if t.exit_time else None,
            exit_reason=t.exit_reason,
        )
        for t in result.scalars().all()
    ]


@router.get("/chart-data")
async def get_trades_chart_data(
    mode: str = "paper",
    instrument: str = "ES",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return recent OHLCV candles + trade markers for paper/live chart."""
    from datetime import datetime, timedelta, timezone

    # Get trades for this mode/instrument
    query = (
        select(Trade)
        .where(Trade.user_id == current_user.id, Trade.mode == mode, Trade.instrument == instrument)
        .order_by(Trade.entry_time.asc())
        .limit(200)
    )
    result = await db.execute(query)
    all_trades = result.scalars().all()

    # Determine date range from trades, or last 7 days if no trades
    now = datetime.now(timezone.utc)
    if all_trades and all_trades[0].entry_time:
        start = all_trades[0].entry_time - timedelta(hours=6)
    else:
        start = now - timedelta(days=7)
    end = now

    # Fetch candles
    from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
    df = await fetch_futures_data(instrument, start, end, "15m")
    candles = []
    if df is not None and not df.empty:
        for ts, row in df.iterrows():
            candles.append({
                "time": int(ts.timestamp()),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            })

    # Build markers from trades
    markers = []
    for t in all_trades:
        if t.entry_time and t.entry_price:
            markers.append({
                "time": int(t.entry_time.timestamp()),
                "type": "entry",
                "direction": t.direction,
                "price": t.entry_price,
                "is_winner": (t.net_pnl or 0) > 0,
            })
        if t.exit_time and t.exit_price:
            markers.append({
                "time": int(t.exit_time.timestamp()),
                "type": "exit",
                "direction": t.direction,
                "price": t.exit_price,
                "is_winner": (t.net_pnl or 0) > 0,
            })

    return {"candles": candles, "markers": markers}


@router.get("/open-positions")
async def get_open_positions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.engines.paper_trading.runner import get_open_positions as _get_open
    from sqlalchemy import text, bindparam
    import uuid as _uuid

    positions = _get_open()
    if not positions:
        return []

    # The paper runner keys traders as "<session_uuid>:<instrument>", so each
    # p["session_id"] is a COMPOSITE string, not a bare UUID. Feeding it raw into
    # a uuid column crashed asyncpg (invalid input syntax for type uuid:
    # "<uuid>:ES"). Extract the real session UUID, drop anything malformed, and
    # use a parameterized, expanding IN clause (the old f-string was also a SQL
    # injection foot-gun).
    def _clean_sid(raw):
        return str(raw).split(":", 1)[0]

    clean_ids = []
    for pos in positions:
        cid = _clean_sid(pos.get("session_id"))
        try:
            _uuid.UUID(cid)
        except (ValueError, TypeError, AttributeError):
            continue
        clean_ids.append(cid)
    if not clean_ids:
        return []

    stmt = text(
        "SELECT id FROM trade_sessions WHERE user_id = :uid AND id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    result = await db.execute(stmt, {"uid": str(current_user.id), "ids": clean_ids})
    user_sessions = {str(r[0]) for r in result.fetchall()}

    return [p for p in positions if _clean_sid(p.get("session_id")) in user_sessions]
