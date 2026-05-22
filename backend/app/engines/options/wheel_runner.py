"""Wheel strategy runner — one task per (session × underlying).

Different shape from the directional options runner because the Wheel
doesn't need ICT signals: it's a time-based state machine that operates
on the spot price alone. Every 60s we:
  1. Pull the latest underlying spot from Polygon
  2. Refresh the chain if needed (6h TTL)
  3. Tick the state machine via `wheel.on_spot_tick(spot, now)`
  4. Persist any completed leg to the trades table

Supports paper (no broker) and live (Tradier) modes.
"""
import asyncio
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from loguru import logger

import pandas as pd
from sqlalchemy import select, text

from app.database import async_session_factory
from app.models.strategy import Strategy
from app.models.user import BrokerAccount
from app.models.trade import Trade, TradingMode, TradeStatus
from app.engines.options.wheel_strategy import WheelStrategy
from app.engines.options.polygon_options import OptionContract
from app.engines.options.options_runner import _fetch_underlying_bars, _polygon_today, _get_chain
from app.engines.live_trading.broker_factory import build_broker_from_account


_active: dict[tuple[str, str], asyncio.Task] = {}


async def _persist_leg(session_id: str, strategy_id: str, user_id: str,
                        broker_account_id: Optional[str], leg) -> None:
    """Write a completed wheel leg to the trades table. Direction stores
    the option side (put / call), and we encode the leg phase in notes so
    the sessions detail page can show 'CSP' / 'CC' tags."""
    try:
        async with async_session_factory() as db:
            t = Trade(
                strategy_id=strategy_id, user_id=user_id,
                broker_account_id=broker_account_id, session_id=session_id,
                mode=(TradingMode.LIVE if broker_account_id else TradingMode.PAPER),
                status=TradeStatus.CLOSED,
                instrument=leg.contract_ticker or "WHEEL",
                direction=("put" if leg.phase == "selling_put" else "call"),
                contracts=leg.contracts,
                entry_price=leg.entry_premium, exit_price=leg.exit_premium,
                stop_loss=0.0, take_profit=0.0,
                entry_time=leg.entry_time, exit_time=leg.exit_time,
                pnl=leg.gross_pnl, commission=leg.commission,
                net_pnl=leg.net_pnl, exit_reason=leg.exit_reason,
                notes={"phase": leg.phase, "is_wheel": True, **leg.metadata},
            )
            db.add(t)
            await db.commit()
    except Exception as e:
        logger.error(f"[WheelRunner] persist failed: {e}")


async def _run_session_loop(session_id: str, strategy_id: str, user_id: str,
                              underlying: str, mode: str,
                              broker_account_id: Optional[str],
                              starting_balance: float):
    logger.info(f"[WheelRunner] start | session={session_id} | {underlying} | mode={mode}")

    # Load strategy
    async with async_session_factory() as db:
        strat = (await db.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one_or_none()
        if not strat:
            logger.error("[WheelRunner] strategy not found")
            return
        broker = None
        if mode == "live":
            acct = (await db.execute(select(BrokerAccount).where(BrokerAccount.id == broker_account_id))).scalar_one_or_none()
            if not acct or (acct.broker or "").lower() != "tradier":
                logger.error("[WheelRunner] Wheel live mode requires a Tradier account")
                return
            broker = build_broker_from_account(acct)

    dte_min = int(getattr(strat, "options_min_dte", 30) or 30)
    dte_max = int(getattr(strat, "options_max_dte", 45) or 45)
    target_delta = float(getattr(strat, "options_target_delta_min", 0.30) or 0.30) + 0.05  # wheel uses ~0.30
    avoid_days = int(getattr(strat, "options_avoid_earnings_days", 0) or 0)

    # Pull chain — both sides because wheel needs puts + calls
    calls = await _get_chain(underlying, "call", dte_min, dte_max)
    puts  = await _get_chain(underlying, "put",  dte_min, dte_max)
    chain = calls + puts
    if not chain:
        logger.error(f"[WheelRunner] empty chain for {underlying}")
        return

    wheel = WheelStrategy(
        underlying=underlying, broker=broker, chain=chain,
        starting_balance=starting_balance, target_delta=target_delta,
        dte_min=dte_min, dte_max=dte_max,
        session_id=session_id, user_id=user_id, strategy_id=strategy_id,
    )
    await wheel.start()

    chain_refreshed_at = datetime.now(timezone.utc)
    try:
        while True:
            try:
                bars = await _fetch_underlying_bars(underlying, lookback_days=1, interval="5m")
                if bars.empty:
                    await asyncio.sleep(60); continue
                spot = float(bars["close"].iloc[-1])

                # Refresh chain every 6h
                if (datetime.now(timezone.utc) - chain_refreshed_at).total_seconds() > 6 * 3600:
                    calls = await _get_chain(underlying, "call", dte_min, dte_max)
                    puts  = await _get_chain(underlying, "put",  dte_min, dte_max)
                    wheel.chain = calls + puts
                    chain_refreshed_at = datetime.now(timezone.utc)

                leg = await wheel.on_spot_tick(spot=spot, now=datetime.now(timezone.utc),
                                                 avoid_earnings_days=avoid_days)
                if leg:
                    await _persist_leg(session_id, strategy_id, user_id,
                                        broker_account_id, leg)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[WheelRunner] tick error: {e}")
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info(f"[WheelRunner] cancelled | session={session_id}")
    finally:
        await wheel.stop()
        if broker:
            try:
                await broker.disconnect()
            except Exception:
                pass


async def start_wheel_session(session_id: str, strategy_id: str, user_id: str,
                                underlyings: list[str], mode: str = "paper",
                                broker_account_id: Optional[str] = None,
                                starting_balance: float = 25_000.0):
    for u in underlyings:
        key = (session_id, u.upper())
        if key in _active and not _active[key].done():
            continue
        task = asyncio.create_task(_run_session_loop(
            session_id, strategy_id, user_id, u.upper(), mode, broker_account_id, starting_balance,
        ))
        _active[key] = task
        logger.info(f"[WheelRunner] spawned for {u}")


async def stop_wheel_session(session_id: str):
    keys = [k for k in _active.keys() if k[0] == session_id]
    for k in keys:
        task = _active.pop(k, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
