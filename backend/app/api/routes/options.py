"""Options-related API surface.

GET /options/preview-strike — show what the strike picker would pick today
                              for a given strategy. Used by the strategy
                              builder so the user can see "if I run this
                              right now, you'd buy SPY 510C 30DTE @ ~$5.20".
"""
from typing import Optional
from datetime import date, datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.database import get_db
from app.models.user import User
from app.models.strategy import Strategy
from app.core.auth import require_2fa_when_paid as get_current_user, require_live_trading
from app.engines.options.polygon_options import PolygonOptionsClient
from app.engines.options.polygon_throttle import gate as _poly_gate
from app.engines.options.strike_picker import pick_strike
from app.engines.options.pricing import price as bs_price, greeks


router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription


# ── Chain cache for the preview endpoint ─────────────────────────────────
# Map (underlying, side, dte_min, dte_max) → (fetched_at, contracts)
# TTLCache (was a bare dict): the key here is USER-CONTROLLED — any
# underlying × side × dte band an authenticated caller sends mints a new
# entry, so a bare dict grows without bound. maxsize=256 caps it and
# expired entries are pruned on set. The manual _PREVIEW_CHAIN_TTL
# freshness check below is unchanged (same get/set semantics).
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
from app.core.ttl_cache import TTLCache
_preview_chain_cache: TTLCache = TTLCache(maxsize=256, ttl_seconds=3600)
_PREVIEW_CHAIN_TTL = _td(hours=1)


async def _cached_chain(client, underlying: str, side: str,
                          dte_min: int, dte_max: int, today):
    """Pull (and cache) the chain. Shared across every preview request so
    a page with 10 options strategy cards on SPY only costs 1 Polygon call."""
    key = (underlying.upper(), side, int(dte_min), int(dte_max))
    now = _dt.now(_tz.utc)
    hit = _preview_chain_cache.get(key)
    if hit and (now - hit[0]) < _PREVIEW_CHAIN_TTL:
        return hit[1]
    await _poly_gate.acquire()
    contracts = await client.list_contracts(
        underlying=underlying.upper(), right=side,
        expiration_after=today + timedelta(days=int(dte_min)),
        expiration_before=today + timedelta(days=int(dte_max)),
        limit=250,
    )
    _preview_chain_cache[key] = (now, contracts)
    return contracts


@router.get("/preview-strike/{strategy_id}")
async def preview_strike(
    strategy_id: str,
    underlying: str = "SPY",
    spot: float = None,                       # if not provided, fetched from Polygon (TODO)
    iv_assumption: float = 0.30,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pull the chain for `underlying`, ask the strike picker which strike
    *this strategy's* config would pick today, and return the resulting
    contract + theoretical premium + greeks."""
    strat = (await db.execute(
        select(Strategy).where(Strategy.id == strategy_id,
                                Strategy.user_id == current_user.id)
    )).scalar_one_or_none()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found")

    # Default to a sensible spot if none supplied. We'd swap to a live quote
    # from Polygon /v2/last/trade once we wire that, but for now we let the
    # caller supply spot to make this preview test-friendly.
    if spot is None:
        # Bug #22 fix: pull real last-close from candle_cache; 422 if missing
        from sqlalchemy import text as _text
        try:
            _row = (await db.execute(
                _text("SELECT close FROM candle_cache WHERE symbol = :sym ORDER BY timestamp DESC LIMIT 1"),
                {"sym": underlying.upper()},
            )).fetchone()
            if _row and _row.close:
                spot = float(_row.close)
            else:
                raise HTTPException(status_code=422, detail=f"Spot price unavailable for {underlying}.")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=422, detail=f"Spot price unavailable for {underlying}.")

    side = "call"
    if getattr(strat, "options_mode", "") == "trend_pullback":
        # Inherit from rule_tree.bias if set
        bias = (strat.rule_tree or {}).get("bias", "bullish")
        side = "call" if bias == "bullish" else "put"

    today = date.today()
    client = PolygonOptionsClient()
    try:
        chain = await _cached_chain(
            client, underlying, side,
            dte_min=int(getattr(strat, "options_min_dte", 30) or 30),
            dte_max=int(getattr(strat, "options_max_dte", 60) or 60),
            today=today,
        )
    except Exception as e:
        msg = str(e)
        if "rate-limit" in msg.lower() or "429" in msg:
            raise HTTPException(status_code=429,
                detail="Polygon free-tier rate-limit hit. Wait ~60 seconds for the bucket to refill. Upgrading the Polygon plan removes this throttle.")
        raise HTTPException(status_code=502, detail=f"Polygon chain fetch failed: {e}")

    if not chain:
        raise HTTPException(status_code=404, detail=f"No {side} contracts found for {underlying} in the configured DTE band.")

    pick = pick_strike(
        chain=chain, spot=spot, today=today, side=side,
        delta_min=getattr(strat, "options_target_delta_min", 0.30) or 0.30,
        delta_max=getattr(strat, "options_target_delta_max", 0.50) or 0.50,
        dte_min=getattr(strat, "options_min_dte", 30) or 30,
        dte_max=getattr(strat, "options_max_dte", 60) or 60,
        prefer_itm=bool(getattr(strat, "options_prefer_itm", False)),
        spread_width=(getattr(strat, "options_spread_width", None) if getattr(strat, "options_mode", "") == "vertical_spread" else None),
        default_iv=iv_assumption,
    )
    if pick is None:
        raise HTTPException(status_code=404, detail="No suitable contract — chain too sparse for this DTE band.")

    t = pick.days_to_expiration / 365.0
    g = greeks(s=spot, k=pick.long.strike, t=t, sigma=iv_assumption,
                opt_type=side)

    cost_per_contract = round(g.price * 100, 2)
    short_payload = None
    if pick.short:
        sg = greeks(s=spot, k=pick.short.strike, t=t, sigma=iv_assumption,
                     opt_type=side)
        short_payload = {
            "ticker": pick.short.ticker, "strike": pick.short.strike,
            "expiration": pick.short.expiration.isoformat(),
            "right": pick.short.right,
            "theoretical_premium": round(sg.price, 2),
            "delta": round(sg.delta, 3),
        }

    return {
        "underlying": underlying,
        "spot": spot,
        "side": side,
        "strategy_name": strat.name,
        "config": {
            "delta_band": [
                getattr(strat, "options_target_delta_min", 0.30),
                getattr(strat, "options_target_delta_max", 0.50),
            ],
            "dte_band": [
                getattr(strat, "options_min_dte", 30),
                getattr(strat, "options_max_dte", 60),
            ],
            "prefer_itm": bool(getattr(strat, "options_prefer_itm", False)),
            "options_mode": getattr(strat, "options_mode", None),
            "spread_width": getattr(strat, "options_spread_width", None),
        },
        "iv_assumption_used": iv_assumption,
        "pick": {
            "long": {
                "ticker": pick.long.ticker,
                "strike": pick.long.strike,
                "expiration": pick.long.expiration.isoformat(),
                "right": pick.long.right,
                "theoretical_premium": round(g.price, 2),
                "delta": round(g.delta, 3),
                "gamma": round(g.gamma, 4),
                "theta": round(g.theta, 3),
                "vega":  round(g.vega, 3),
                "cost_per_contract_usd": cost_per_contract,
            },
            "short": short_payload,
            "days_to_expiration": pick.days_to_expiration,
            "band_missed": pick.band_missed,
            "reason": pick.reason,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Options paper sessions
# ─────────────────────────────────────────────────────────────────────────────

from pydantic import BaseModel
from app.models.trade import TradeSession, TradingMode
from app.api.routes.legal import require_current_ack
from app.engines.options.options_runner import (
    start_options_session, stop_options_session,
)


class StartOptionsSessionRequest(BaseModel):
    strategy_id: str
    underlyings: list[str]                       # e.g. ["SPY", "QQQ"]
    starting_balance: float = 10_000.0
    mode: str = "paper"                          # "paper" or "live"
    broker_account_id: Optional[str] = None      # required when mode == "live"


@router.post("/sessions", status_code=201)
async def start_options_session_endpoint(
    data: StartOptionsSessionRequest,
    current_user: User = Depends(require_live_trading),
    db: AsyncSession = Depends(get_db),
):
    """Start an options trading session.

    `mode="paper"` runs the BS-priced paper engine (no broker connection).
    `mode="live"` connects a Tradier broker account and routes real orders
    (sandbox or production, depending on the account's sandbox_mode flag).
    """
    mode = (data.mode or "paper").lower()

    # Options consent gate — required for both paper *and* live so the user
    # has read and accepted the options-specific risk disclosure exactly once.
    await require_current_ack(db, current_user.id, "options_trading_consent")

    # Live mode additionally requires risk + live-trading consent
    if mode == "live":
        await require_current_ack(db, current_user.id, "risk_disclosure")
        await require_current_ack(db, current_user.id, "live_trading_consent")

    # Verify strategy ownership
    from sqlalchemy import select, text
    strat = (await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id,
                                Strategy.user_id == current_user.id)
    )).scalar_one_or_none()
    if not strat:
        raise HTTPException(status_code=404, detail="Strategy not found")

    broker_account_id = None
    if mode == "live":
        if not data.broker_account_id:
            raise HTTPException(status_code=400, detail="broker_account_id is required for live mode.")
        from app.models.user import BrokerAccount
        acct = (await db.execute(
            select(BrokerAccount).where(BrokerAccount.id == data.broker_account_id,
                                         BrokerAccount.user_id == current_user.id)
        )).scalar_one_or_none()
        if not acct:
            raise HTTPException(status_code=404, detail="Broker account not found.")
        if (acct.broker or "").lower() != "tradier":
            raise HTTPException(status_code=400,
                                 detail="Options live trading requires a Tradier account. Connect one in Live Trading.")
        broker_account_id = str(acct.id)

    # Insert a TradeSession row
    session = TradeSession(
        strategy_id=strat.id,
        user_id=current_user.id,
        broker_account_id=broker_account_id,
        mode=TradingMode.LIVE if mode == "live" else TradingMode.PAPER,
        is_active=True,
        instrument=",".join(data.underlyings),
        label=f"options:{mode}:{','.join(data.underlyings)}",
    )
    db.add(session)
    await db.flush()
    sid = str(session.id)
    await db.commit()

    # Spawn the right runner — wheel mode is its own engine (sells premium
    # instead of buying directional options).
    is_wheel = (getattr(strat, "options_mode", "") or "") == "wheel"
    if is_wheel:
        from app.engines.options.wheel_runner import start_wheel_session
        await start_wheel_session(
            session_id=sid, strategy_id=str(strat.id),
            user_id=str(current_user.id),
            underlyings=data.underlyings, mode=mode,
            broker_account_id=broker_account_id,
            starting_balance=data.starting_balance,
        )
    elif mode == "live":
        from app.engines.options.options_live_runner import start_live_options_session
        await start_live_options_session(
            session_id=sid, strategy_id=str(strat.id),
            user_id=str(current_user.id), broker_account_id=broker_account_id,
            underlyings=data.underlyings, starting_balance=data.starting_balance,
        )
    else:
        await start_options_session(
            session_id=sid, strategy_id=str(strat.id),
            user_id=str(current_user.id),
            underlyings=data.underlyings,
            starting_balance=data.starting_balance,
        )

    return {"session_id": sid, "status": "started", "mode": mode,
            "underlyings": data.underlyings,
            "broker_account_id": broker_account_id,
            "starting_balance": data.starting_balance}


@router.post("/sessions/{session_id}/stop")
async def stop_options_session_endpoint(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Stop all runners for an options session, regardless of paper or live."""
    from sqlalchemy import select, text
    sess = (await db.execute(
        select(TradeSession).where(TradeSession.id == session_id,
                                     TradeSession.user_id == current_user.id)
    )).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")
    # Try all runners — only the active one will actually do anything
    await stop_options_session(session_id)
    try:
        from app.engines.options.options_live_runner import stop_live_options_session
        await stop_live_options_session(session_id)
    except Exception:
        pass
    try:
        from app.engines.options.wheel_runner import stop_wheel_session
        await stop_wheel_session(session_id)
    except Exception:
        pass
    sess.is_active = False
    sess.ended_at = datetime.now(timezone.utc)
    await db.commit()
    return {"session_id": session_id, "status": "stopped"}


@router.get("/sessions")
async def list_my_options_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the user's options paper sessions (active + historical)."""
    rows = (await db.execute(text("""
        SELECT id, strategy_id, instrument, label, started_at, ended_at,
               is_active, total_trades, net_pnl
          FROM trade_sessions
         WHERE user_id = :uid AND mode = 'paper'
           AND label LIKE 'options:%'
         ORDER BY started_at DESC
         LIMIT 50
    """), {"uid": str(current_user.id)})).all()
    return {"sessions": [
        {
            "session_id": str(r.id), "strategy_id": str(r.strategy_id) if r.strategy_id else None,
            "underlyings": (r.instrument or "").split(","),
            "label": r.label, "started_at": r.started_at.isoformat() if r.started_at else None,
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "is_active": bool(r.is_active),
            "total_trades": int(r.total_trades or 0),
            "net_pnl": float(r.net_pnl or 0),
        } for r in rows
    ]}


@router.get("/sessions/{session_id}")
async def get_options_session_detail(
    session_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Detail view: session metadata + recent trades + open position info."""
    from sqlalchemy import select, text
    sess = (await db.execute(
        select(TradeSession).where(TradeSession.id == session_id,
                                     TradeSession.user_id == current_user.id)
    )).scalar_one_or_none()
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found")

    trades_rows = (await db.execute(text("""
        SELECT id, instrument, direction, contracts,
               entry_price, exit_price, stop_loss, take_profit,
               entry_time, exit_time, pnl, commission, net_pnl,
               exit_reason, status, notes
          FROM trades
         WHERE session_id = :sid AND user_id = :uid
         ORDER BY entry_time DESC
         LIMIT 200
    """), {"sid": session_id, "uid": str(current_user.id)})).all()

    trades = []
    for r in trades_rows:
        trades.append({
            "id": str(r.id),
            "instrument": r.instrument,
            "direction": r.direction,
            "contracts": r.contracts,
            "entry_price": float(r.entry_price) if r.entry_price is not None else None,
            "exit_price": float(r.exit_price) if r.exit_price is not None else None,
            "stop_loss": float(r.stop_loss) if r.stop_loss is not None else None,
            "take_profit": float(r.take_profit) if r.take_profit is not None else None,
            "entry_time": r.entry_time.isoformat() if r.entry_time else None,
            "exit_time": r.exit_time.isoformat() if r.exit_time else None,
            "pnl": float(r.pnl) if r.pnl is not None else None,
            "commission": float(r.commission) if r.commission is not None else None,
            "net_pnl": float(r.net_pnl) if r.net_pnl is not None else None,
            "exit_reason": r.exit_reason,
            "status": r.status,
            "notes": r.notes or {},
        })

    return {
        "session_id": str(sess.id),
        "strategy_id": str(sess.strategy_id) if sess.strategy_id else None,
        "mode": sess.mode.value if hasattr(sess.mode, "value") else sess.mode,
        "underlyings": (sess.instrument or "").split(","),
        "label": sess.label,
        "is_active": bool(sess.is_active),
        "started_at": sess.started_at.isoformat() if sess.started_at else None,
        "ended_at": sess.ended_at.isoformat() if sess.ended_at else None,
        "total_trades": int(sess.total_trades or 0),
        "net_pnl": float(sess.net_pnl or 0),
        "broker_account_id": str(sess.broker_account_id) if sess.broker_account_id else None,
        "trades": trades,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Pending trades — pre-market confirm/decline flow
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/pending/{confirm_token}")
async def get_pending_trade_by_token(confirm_token: str, db: AsyncSession = Depends(get_db)):
    """Lookup a pending trade by its one-time token. Used by the confirm
    page to show the user what they're about to confirm."""
    r = (await db.execute(text("""
        SELECT pt.*, s.name AS strategy_name
          FROM pending_trades pt
          JOIN strategies s ON s.id = pt.strategy_id
         WHERE pt.confirm_token = :t
    """), {"t": confirm_token})).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="Pending trade not found or token expired.")
    m = dict(r._mapping)
    return {
        "id": str(m["id"]),
        "strategy_name": m["strategy_name"],
        "instrument": m["instrument"],
        "direction": m["direction"],
        "contracts": m["contracts"],
        "entry_price": m["entry_price"],
        "stop_loss":   m["stop_loss"],
        "take_profit": m["take_profit"],
        "bias":   m["bias"],
        "reason": m["reason"],
        "status": m["status"],
        "expires_at": m["expires_at"].isoformat() if m["expires_at"] else None,
        "is_intraday": bool(m["is_intraday"]),
    }


@router.post("/pending/{confirm_token}/confirm")
async def confirm_pending(confirm_token: str, db: AsyncSession = Depends(get_db)):
    from app.engines.options.pending_trades import confirm_pending_trade
    row = await confirm_pending_trade(confirm_token)
    if not row:
        raise HTTPException(status_code=410,
                             detail="This signal already expired or was already acted on.")
    # Fire-and-forget — caller runner picks up confirmed rows on its next tick.
    # For paper mode we can also kick it directly here, but the runner-poll
    # pattern keeps the confirm endpoint snappy.
    return {"status": "confirmed", "id": str(row["id"]),
            "instrument": row["instrument"], "direction": row["direction"]}


@router.post("/pending/{confirm_token}/decline")
async def decline_pending(confirm_token: str, db: AsyncSession = Depends(get_db)):
    from app.engines.options.pending_trades import decline_pending_trade
    ok = await decline_pending_trade(confirm_token)
    if not ok:
        raise HTTPException(status_code=410, detail="This signal already expired or was acted on.")
    return {"status": "declined"}


@router.get("/pending")
async def list_my_pending(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Pending trades dashboard for the current user."""
    rows = (await db.execute(text("""
        SELECT pt.id, pt.strategy_id, s.name AS strategy_name,
               pt.instrument, pt.direction, pt.contracts,
               pt.entry_price, pt.stop_loss, pt.take_profit,
               pt.bias, pt.reason, pt.status, pt.is_intraday,
               pt.created_at, pt.expires_at, pt.confirmed_at, pt.executed_at
          FROM pending_trades pt
          JOIN strategies s ON s.id = pt.strategy_id
         WHERE pt.user_id = :uid
         ORDER BY pt.created_at DESC
         LIMIT 50
    """), {"uid": str(current_user.id)})).all()
    return {"pending_trades": [
        {
            "id": str(r.id), "strategy_id": str(r.strategy_id),
            "strategy_name": r.strategy_name,
            "instrument": r.instrument, "direction": r.direction,
            "contracts": r.contracts,
            "entry_price": float(r.entry_price) if r.entry_price is not None else None,
            "stop_loss":   float(r.stop_loss)   if r.stop_loss   is not None else None,
            "take_profit": float(r.take_profit) if r.take_profit is not None else None,
            "bias": r.bias, "reason": r.reason, "status": r.status,
            "is_intraday": bool(r.is_intraday),
            "created_at":   r.created_at.isoformat()   if r.created_at   else None,
            "expires_at":   r.expires_at.isoformat()   if r.expires_at   else None,
            "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
            "executed_at":  r.executed_at.isoformat()  if r.executed_at  else None,
        } for r in rows
    ]}
