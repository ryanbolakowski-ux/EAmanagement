from datetime import datetime
from typing import Optional

from app.engines.paper_trading.runner import start_paper_session as _start_runner, stop_paper_session as _stop_runner
from app.engines.paper_trading.allocation import clamp_starting_balance
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, func, case, delete
from pydantic import BaseModel

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.models.strategy import Strategy
from app.models.trade import TradeSession, TradingMode, Trade, TradeStatus
from app.core.auth import require_2fa_when_paid as get_current_user, require_tier

router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription

eligible_tiers = [SubscriptionTier.FREE_TRIAL, SubscriptionTier.TIER_1, SubscriptionTier.TIER_3, SubscriptionTier.TIER_4, SubscriptionTier.TIER_5]


class StartPaperSessionRequest(BaseModel):
    strategy_id: str
    # Either `instruments: ["ES", "NQ"]` (preferred) or singular `instrument: "ES"`.
    # If both are missing we default to ["ES"]. Multiple instruments share one session.
    instruments: Optional[list[str]] = None
    instrument: Optional[str] = None
    daily_loss_limit: Optional[float] = None
    max_trades_today: Optional[int] = None

    def resolved_instruments(self) -> list[str]:
        if self.instruments:
            seen, out = set(), []
            for i in self.instruments:
                if i and i not in seen:
                    seen.add(i)
                    out.append(i)
            return out
        if self.instrument:
            return [self.instrument]
        return ["ES"]


class SessionResponse(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    mode: str
    is_active: bool
    started_at: str
    instrument: Optional[str] = None
    label: Optional[str] = None
    total_trades: int
    wins: int = 0
    losses: int = 0
    net_pnl: float
    # Paper-engine capital allocation (ALLOC-V1). Applies when the session's
    # runner (re)starts; None on rows created before the column existed.
    starting_balance: Optional[float] = None


class LabelUpdate(BaseModel):
    label: Optional[str] = None


class SessionTradeRow(BaseModel):
    id: str
    instrument: str
    direction: str
    status: str
    entry_price: Optional[float]
    exit_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    contracts: int
    pnl: Optional[float]
    net_pnl: Optional[float]
    entry_time: Optional[str]
    exit_time: Optional[str]
    exit_reason: Optional[str]


class SessionMetrics(BaseModel):
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: Optional[float]
    avg_win: float
    avg_loss: float
    max_drawdown: float
    max_drawdown_pct: float
    largest_win: float
    largest_loss: float


class SessionDetail(BaseModel):
    session: SessionResponse
    metrics: SessionMetrics
    trades: list[SessionTradeRow]


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

    # Paper trading is FUTURES-ONLY. Options strategies must use the
    # Tradier sandbox path on /app/live (real broker API in sandbox mode,
    # which simulates fills against real market data).
    _OPT_TICKERS = {"SPY","QQQ","NVDA","AAPL","MSFT","TSLA","AMD","META","AMZN","GOOGL","JPM","KO"}
    _strat_instr = set(strategy.instruments or [])
    if getattr(strategy, "options_mode", None) or (_strat_instr & _OPT_TICKERS):
        raise HTTPException(
            status_code=400,
            detail="Paper trading is futures-only (ES/NQ/RTY/YM). Options strategies use the Tradier sandbox path on Live Trading.",
        )

    instruments = data.resolved_instruments()

    # Reject if any of the requested instruments is already covered by an
    # active session for this strategy. We compare by overlap with the stored
    # comma-joined `instrument` string.
    existing = await db.execute(
        select(TradeSession).where(
            TradeSession.user_id == current_user.id,
            TradeSession.strategy_id == strategy.id,
            TradeSession.mode == TradingMode.PAPER,
            TradeSession.is_active == True,
        )
    )
    for sess in existing.scalars().all():
        running = set((sess.instrument or "").split(","))
        clash = sorted(running & set(instruments))
        if clash:
            raise HTTPException(
                status_code=400,
                detail=f"A session for {strategy.name} on {', '.join(clash)} is already running. Stop it first.",
            )

    session = TradeSession(
        strategy_id=strategy.id,
        user_id=current_user.id,
        mode=TradingMode.PAPER,
        is_active=True,
        instrument=",".join(instruments),
        daily_loss_limit=data.daily_loss_limit,
        max_trades_today=data.max_trades_today,
    )
    db.add(session)
    await db.commit()

    # Spin up one runner per instrument under the same session_id.
    for inst in instruments:
        await _start_runner(str(session.id), str(session.strategy_id), str(current_user.id), inst)

    return SessionResponse(
        id=str(session.id), strategy_id=str(session.strategy_id),
        strategy_name=strategy.name,
        mode=session.mode, is_active=session.is_active,
        started_at=session.started_at.isoformat(),
        instrument=session.instrument,
        label=session.label,
        total_trades=session.total_trades, wins=0, losses=0,
        net_pnl=session.net_pnl,
    )


@router.get("/sessions", response_model=list[SessionResponse])
async def list_paper_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all paper sessions for this user with per-session win/loss stats.

    The wins/losses are computed by joining trade_sessions to closed trades and
    counting positive vs negative net_pnl.
    """
    # Compute stats from the trades table directly so cached session.total_trades
    # / session.net_pnl can't drift away from reality (multi-instrument runners
    # racing on the cached fields had been overwriting each other).
    win_expr   = func.coalesce(func.sum(case((Trade.net_pnl > 0, 1), else_=0)), 0).label("wins")
    loss_expr  = func.coalesce(func.sum(case((Trade.net_pnl < 0, 1), else_=0)), 0).label("losses")
    total_expr = func.coalesce(func.count(Trade.id), 0).label("total_closed")
    pnl_expr   = func.coalesce(func.sum(Trade.net_pnl), 0.0).label("computed_pnl")

    stmt = (
        select(TradeSession, Strategy.name, win_expr, loss_expr, total_expr, pnl_expr)
        .join(Strategy, TradeSession.strategy_id == Strategy.id)
        .outerjoin(Trade, (Trade.session_id == TradeSession.id) & (Trade.status == TradeStatus.CLOSED))
        .where(
            TradeSession.user_id == current_user.id,
            TradeSession.mode == TradingMode.PAPER,
        )
        .group_by(TradeSession.id, Strategy.name)
        .order_by(TradeSession.started_at.desc())
    )
    rows = (await db.execute(stmt)).all()

    return [
        SessionResponse(
            id=str(s.id), strategy_id=str(s.strategy_id),
            strategy_name=strategy_name,
            mode=s.mode, is_active=s.is_active,
            started_at=s.started_at.isoformat(),
            instrument=s.instrument,
            label=s.label,
            total_trades=int(total_closed or 0),
            wins=int(wins or 0), losses=int(losses or 0),
            net_pnl=float(computed_pnl or 0.0),
            starting_balance=float(s.starting_balance) if s.starting_balance is not None else None,
        )
        for s, strategy_name, wins, losses, total_closed, computed_pnl in rows
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
    session.ended_at = datetime.utcnow()
    await _stop_runner(session_id)
    await db.commit()
    return {"status": "stopped"}


@router.post("/sessions/stop-all")
async def stop_all_paper_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop every active paper session for this user."""
    result = await db.execute(
        select(TradeSession).where(
            TradeSession.user_id == current_user.id,
            TradeSession.mode == TradingMode.PAPER,
            TradeSession.is_active == True,
        )
    )
    sessions = result.scalars().all()
    now = datetime.utcnow()
    stopped_ids = []
    for s in sessions:
        s.is_active = False
        s.ended_at = now
        await _stop_runner(str(s.id))
        stopped_ids.append(str(s.id))
    await db.commit()
    return {"status": "stopped", "count": len(stopped_ids), "session_ids": stopped_ids}


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_paper_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a paper session and any trades attached to it.

    If the session is still active, stop the background runner first so it
    can't write into a row we're about to delete.
    """
    result = await db.execute(
        select(TradeSession).where(TradeSession.id == session_id, TradeSession.user_id == current_user.id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    if session.is_active:
        try:
            await _stop_runner(session_id)
        except Exception:
            pass
        session.is_active = False

    # Trades.session_id is nullable, but for paper sessions we want a hard delete.
    await db.execute(delete(Trade).where(Trade.session_id == session.id))
    await db.delete(session)
    await db.commit()
    return None


@router.patch("/sessions/{session_id}/label", response_model=SessionResponse)
async def set_session_label(
    session_id: str,
    data: LabelUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TradeSession, Strategy.name)
        .join(Strategy, TradeSession.strategy_id == Strategy.id)
        .where(TradeSession.id == session_id, TradeSession.user_id == current_user.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    session, strategy_name = row
    new_label = (data.label or "").strip() or None
    if new_label and len(new_label) > 100:
        raise HTTPException(status_code=400, detail="Label must be 100 characters or fewer.")
    session.label = new_label
    await db.commit()
    return SessionResponse(
        id=str(session.id), strategy_id=str(session.strategy_id), strategy_name=strategy_name,
        mode=session.mode, is_active=session.is_active,
        started_at=session.started_at.isoformat(),
        instrument=session.instrument, label=session.label,
        total_trades=session.total_trades, wins=0, losses=0, net_pnl=session.net_pnl,
    )


class AllocationUpdate(BaseModel):
    starting_balance: float


@router.patch("/sessions/{session_id}/allocation")
async def set_session_allocation(
    session_id: str,
    data: AllocationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Set the paper-engine capital allocated to this session (ALLOC-V1).

    Replaces the old duplicate-session "multiplier": the engine sizes
    positions off equity, so raising the starting balance simply sizes up
    each position. Clamped to [$1k, $1M]. Takes effect when the session's
    runner (re)starts.
    """
    result = await db.execute(
        select(TradeSession)
        .where(TradeSession.id == session_id, TradeSession.user_id == current_user.id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")
    _mode = getattr(session, "mode", None)
    _mode = getattr(_mode, "value", _mode)  # Enum or str
    if str(_mode or "").lower() != "paper":
        raise HTTPException(
            status_code=400,
            detail="Allocation applies to paper futures sessions only — live and "
                   "options-paper sessions size from their broker account.")
    try:
        balance = clamp_starting_balance(data.starting_balance)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="starting_balance must be a finite number.")
    session.starting_balance = balance
    await db.commit()
    return {
        "ok": True,
        "starting_balance": balance,
        "note": "applies when the session (re)starts \u2014 a backend deploy tonight restarts all paper sessions",
    }


def _compute_metrics(trades: list[Trade]) -> SessionMetrics:
    """Compute the per-session trading metrics from closed trades."""
    closed = [t for t in trades if t.status == TradeStatus.CLOSED and t.net_pnl is not None]
    total = len(closed)
    wins_list = [float(t.net_pnl) for t in closed if (t.net_pnl or 0) > 0]
    losses_list = [float(t.net_pnl) for t in closed if (t.net_pnl or 0) < 0]
    wins = len(wins_list)
    losses = len(losses_list)
    net_pnl = sum(float(t.net_pnl) for t in closed)
    gross_profit = sum(wins_list)
    gross_loss = sum(losses_list)  # negative
    win_rate = (wins / total) if total > 0 else 0.0
    profit_factor = (gross_profit / abs(gross_loss)) if gross_loss != 0 else None
    avg_win = (gross_profit / wins) if wins > 0 else 0.0
    avg_loss = (gross_loss / losses) if losses > 0 else 0.0
    largest_win = max(wins_list) if wins_list else 0.0
    largest_loss = min(losses_list) if losses_list else 0.0

    # Drawdown: walk the ordered closed trades, build running equity, track peak-to-trough
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    peak_at_max_dd = 0.0
    ordered = sorted(closed, key=lambda t: t.exit_time or t.entry_time or t.created_at)
    for t in ordered:
        equity += float(t.net_pnl)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
            peak_at_max_dd = peak
    max_dd_pct = (max_dd / peak_at_max_dd * 100.0) if peak_at_max_dd > 0 else 0.0

    return SessionMetrics(
        total_trades=total, wins=wins, losses=losses,
        win_rate=round(win_rate, 4),
        net_pnl=round(net_pnl, 2),
        gross_profit=round(gross_profit, 2),
        gross_loss=round(gross_loss, 2),
        profit_factor=round(profit_factor, 2) if profit_factor is not None else None,
        avg_win=round(avg_win, 2), avg_loss=round(avg_loss, 2),
        max_drawdown=round(max_dd, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        largest_win=round(largest_win, 2),
        largest_loss=round(largest_loss, 2),
    )


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_paper_session_detail(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(TradeSession, Strategy.name)
        .join(Strategy, TradeSession.strategy_id == Strategy.id)
        .where(TradeSession.id == session_id, TradeSession.user_id == current_user.id)
    )
    row = result.one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Session not found.")
    session, strategy_name = row

    trades_result = await db.execute(
        select(Trade)
        .where(Trade.session_id == session.id)
        .order_by(Trade.entry_time.desc().nullslast(), Trade.created_at.desc())
    )
    trades = list(trades_result.scalars().all())
    metrics = _compute_metrics(trades)

    closed_count = metrics.total_trades
    win_count = metrics.wins
    loss_count = metrics.losses

    return SessionDetail(
        session=SessionResponse(
            id=str(session.id), strategy_id=str(session.strategy_id), strategy_name=strategy_name,
            mode=session.mode, is_active=session.is_active,
            started_at=session.started_at.isoformat(),
            instrument=session.instrument, label=session.label,
            total_trades=closed_count, wins=win_count, losses=loss_count,
            net_pnl=metrics.net_pnl,
        ),
        metrics=metrics,
        trades=[
            SessionTradeRow(
                id=str(t.id), instrument=t.instrument, direction=str(t.direction.value if hasattr(t.direction, 'value') else t.direction),
                status=str(t.status.value if hasattr(t.status, 'value') else t.status),
                entry_price=t.entry_price, exit_price=t.exit_price,
                stop_loss=t.stop_loss, take_profit=t.take_profit,
                contracts=t.contracts,
                pnl=t.pnl, net_pnl=t.net_pnl,
                entry_time=t.entry_time.isoformat() if t.entry_time else None,
                exit_time=t.exit_time.isoformat() if t.exit_time else None,
                exit_reason=t.exit_reason,
            )
            for t in trades
        ],
    )


@router.post("/positions/close-all")
async def close_all_open_positions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Force-close every open paper position belonging to this user at the
    runner's last known price. Each closure becomes a regular closed trade —
    appears in the trade history with exit_reason='manual'.
    """
    from app.engines.paper_trading.runner import _active_traders, _save_new_trades
    from app.engines.strategy_engine.base_strategy import ExitReason
    from datetime import datetime as _dt

    # Collect this user's session ids so we know which traders to act on
    sess_rows = await db.execute(
        select(TradeSession.id).where(
            TradeSession.user_id == current_user.id,
            TradeSession.mode == TradingMode.PAPER,
            TradeSession.is_active == True,
        )
    )
    user_session_ids = {str(s) for (s,) in sess_rows.all()}

    closed = 0
    for key, trader in list(_active_traders.items()):
        # key is "session_id:instrument" or just session_id
        sid = key.split(":")[0]
        if sid not in user_session_ids:
            continue
        if not getattr(trader, "_position", None):
            continue
        last_price = getattr(trader, "_last_price", 0.0) or trader._position.entry_price
        try:
            await trader._close_position(float(last_price), _dt.utcnow(), ExitReason.MANUAL)
            strategy_id = getattr(trader, "strategy_id", None) or ""
            await _save_new_trades(trader, sid, strategy_id, str(current_user.id), trader.instrument)
            closed += 1
        except Exception:
            pass

    return {"status": "ok", "closed": closed}


# CHART-EQUITY-SOURCE-V1 — candle_cache only holds futures; equity trade
# charts must source OHLC from the equity feed (yfinance).
def _chart_is_futures(inst: str) -> bool:
    from app.engines.pnl_marks import is_futures_symbol
    return is_futures_symbol(inst)

def _chart_equity_ohlc(inst, win_start, win_end, tf_min):
    """Equity OHLC window from yfinance, resampled to tf_min, tail(150)."""
    import pandas as _pd
    try:
        import yfinance as _yf
    except Exception:
        return []
    interval = {1: "1m", 5: "5m", 15: "15m", 60: "60m", 240: "60m"}.get(int(tf_min), "5m")
    try:
        df = _yf.Ticker(inst).history(start=win_start, end=win_end,
                                      interval=interval, prepost=False)
        if df is None or df.empty:
            return []
        df = df.rename(columns={"Open": "open", "High": "high",
                                "Low": "low", "Close": "close"})
        df.index = _pd.to_datetime(df.index, utc=True)
        df = df[["open", "high", "low", "close"]]
        if int(tf_min) == 240 and interval == "60m":
            df = df.resample("240min").agg({"open": "first", "high": "max",
                "low": "min", "close": "last"}).dropna()
        return [{"t": ts.isoformat(), "o": float(r["open"]), "h": float(r["high"]),
                 "l": float(r["low"]), "c": float(r["close"])}
                for ts, r in df.tail(150).iterrows()]
    except Exception:
        return []


@router.get("/trades/{trade_id}/chart")
async def get_trade_chart(
    trade_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the chart-context payload the strategy captured at entry, plus
    the trade's entry/exit/SL/TP so the frontend can render a candlestick
    snapshot showing exactly where and why the trade fired."""
    result = await db.execute(
        select(Trade).where(Trade.id == trade_id, Trade.user_id == current_user.id)
    )
    trade = result.scalar_one_or_none()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found.")

    # Notes can come back as either a dict (jsonb native) or a string
    # (legacy rows where the insert wrapped the dict with json.dumps()).
    # Parse defensively so we don't 500 on either shape.
    notes = trade.notes or {}
    if isinstance(notes, str):
        try:
            import json as _json
            notes = _json.loads(notes)
        except Exception:
            notes = {}
    if not isinstance(notes, dict):
        notes = {}
    candles = notes.get("chart_candles") or []

    # Backfill for older trades that were saved before the runner started
    # persisting `notes`. Pull 30 bars before and 30 after the entry from
    # candle_cache so the modal at least shows price action even without
    # the original FVG overlay.
    if not candles and trade.entry_time:
        from datetime import timedelta as _td
        # Detect ~5m bar spacing so 30 bars ≈ 2.5h window each side
        window_min = 150
        win_start = trade.entry_time - _td(minutes=window_min)
        win_end   = (trade.exit_time or trade.entry_time) + _td(minutes=window_min)
        if not _chart_is_futures(trade.instrument):
            # Equity trade — candle_cache has no rows; use the equity feed.
            candles = _chart_equity_ohlc(trade.instrument, win_start, win_end, 5)
            rows = []
        else:
            rows = (await db.execute(text("""
                SELECT timestamp, open, high, low, close FROM candle_cache
                 WHERE instrument = :inst
                   AND timestamp >= :s AND timestamp <= :e
                 ORDER BY timestamp
            """), {"inst": trade.instrument, "s": win_start, "e": win_end})).all()
        # Downsample to 5m bars to keep the payload small
        import pandas as pd
        if rows:
            df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
            df = df.set_index("timestamp").resample("5min").agg({
                "open": "first", "high": "max", "low": "min", "close": "last",
            }).dropna()
            for ts, row in df.iterrows():
                candles.append({
                    "t": ts.isoformat(),
                    "o": float(row["open"]),
                    "h": float(row["high"]),
                    "l": float(row["low"]),
                    "c": float(row["close"]),
                })

    # Build candles_by_tf for the multi-timeframe replay animation.
    # Returns a window around the trade for each major timeframe so the
    # frontend can zoom from 4h → 1m as the animation plays through steps.
    candles_by_tf: dict[str, list] = {}
    if trade.entry_time:
        from datetime import timedelta as _td
        import pandas as _pd
        # Per TF: window before + after entry
        tf_windows = {
            "4h":  (_td(days=20), _td(days=5)),
            "1h":  (_td(days=7),  _td(days=2)),
            "15m": (_td(hours=24), _td(hours=12)),
            "5m":  (_td(hours=8),  _td(hours=6)),
            "1m":  (_td(hours=2),  _td(hours=2)),
        }
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
        for tf, (before, after) in tf_windows.items():
            try:
                win_start = trade.entry_time - before
                win_end   = (trade.exit_time or trade.entry_time) + after
                if not _chart_is_futures(trade.instrument):
                    candles_by_tf[tf] = _chart_equity_ohlc(
                        trade.instrument, win_start, win_end, tf_minutes[tf])
                    continue
                rows = (await db.execute(text("""
                    SELECT timestamp, open, high, low, close FROM candle_cache
                     WHERE instrument = :inst
                       AND timestamp >= :s AND timestamp <= :e
                     ORDER BY timestamp
                """), {"inst": trade.instrument, "s": win_start, "e": win_end})).all()
                if not rows:
                    candles_by_tf[tf] = []
                    continue
                df = _pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close"])
                df["timestamp"] = _pd.to_datetime(df["timestamp"], utc=True)
                df = df.set_index("timestamp")
                if tf != "1m":
                    df = df.resample(f"{tf_minutes[tf]}min").agg({
                        "open":"first","high":"max","low":"min","close":"last",
                    }).dropna()
                # Cap each TF at ~150 bars for payload size
                df = df.tail(150)
                candles_by_tf[tf] = [{
                    "t": ts.isoformat(),
                    "o": float(r["open"]), "h": float(r["high"]),
                    "l": float(r["low"]),  "c": float(r["close"]),
                } for ts, r in df.iterrows()]
            except Exception:
                candles_by_tf[tf] = []

    try:
        from loguru import logger as _lg_chart
        _csrc = "candle_cache" if _chart_is_futures(trade.instrument) else "equity_feed"
        _lg_chart.info(
            f"[chart-load] trade={trade.id} inst={trade.instrument} "
            f"source={_csrc} candles={len(candles)} "
            f"tfs={ {k: len(v) for k, v in candles_by_tf.items()} }"
        )
    except Exception:
        pass
    return {
        "id": str(trade.id),
        "instrument": trade.instrument,
        "direction": str(trade.direction.value if hasattr(trade.direction, "value") else trade.direction),
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "stop_loss": trade.stop_loss,
        "take_profit": trade.take_profit,
        "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "exit_reason": trade.exit_reason,
        "net_pnl": trade.net_pnl,
        "bias": notes.get("bias"),
        "fvg_type": notes.get("fvg_type"),
        "primary_tf": notes.get("primary_tf") or "5m",
        "candles": candles,
        "candles_by_tf": candles_by_tf,
        "fvgs": notes.get("chart_fvgs", []),
    }


# ── Trade Comments (user can mark up a trade chart) ────────────────────

class TradeCommentCreate(BaseModel):
    body: str
    mark_x: float | None = None
    mark_y: float | None = None
    mark_label: str | None = None


@router.get("/trades/{trade_id}/comments")
async def list_trade_comments(
    trade_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List comments on a trade. Trade owner can see all comments + their own."""
    # Verify ownership
    t = (await db.execute(
        select(Trade).where(Trade.id == trade_id, Trade.user_id == current_user.id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Trade not found.")
    rows = await db.execute(text("""
        SELECT id, body, mark_x, mark_y, mark_label, created_at
          FROM trade_comments
         WHERE trade_id = :tid
         ORDER BY created_at DESC
    """), {"tid": trade_id})
    return [
        {
            "id": str(r[0]),
            "body": r[1] or "",
            "mark_x": r[2],
            "mark_y": r[3],
            "mark_label": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows.fetchall()
    ]


@router.post("/trades/{trade_id}/comments", status_code=status.HTTP_201_CREATED)
async def add_trade_comment(
    trade_id: str,
    data: TradeCommentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Add a comment / annotation to a trade. Used by the user to flag where
    the bot went wrong so we can improve the strategy."""
    t = (await db.execute(
        select(Trade).where(Trade.id == trade_id, Trade.user_id == current_user.id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Trade not found.")
    if not (data.body or "").strip():
        raise HTTPException(status_code=400, detail="Comment body cannot be empty.")
    import uuid as _u
    cid = str(_u.uuid4())
    await db.execute(text("""
        INSERT INTO trade_comments (id, trade_id, user_id, body, mark_x, mark_y, mark_label)
        VALUES (:id, :tid, :uid, :body, :mx, :my, :ml)
    """), {
        "id": cid, "tid": trade_id, "uid": str(current_user.id),
        "body": data.body.strip(),
        "mx": data.mark_x, "my": data.mark_y, "ml": (data.mark_label or "").strip() or None,
    })
    await db.commit()
    return {"id": cid, "status": "added"}


@router.delete("/trades/{trade_id}/comments/{comment_id}", status_code=204)
async def delete_trade_comment(
    trade_id: str,
    comment_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    t = (await db.execute(
        select(Trade).where(Trade.id == trade_id, Trade.user_id == current_user.id)
    )).scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Trade not found.")
    await db.execute(text(
        "DELETE FROM trade_comments WHERE id = :cid AND trade_id = :tid AND user_id = :uid"
    ), {"cid": comment_id, "tid": trade_id, "uid": str(current_user.id)})
    await db.commit()
    return None
