"""Live trading session runner — async task that drives a LiveTrader on
real broker-quoted bars.

Mirrors the paper_trading.runner pattern. Bug #5 fix wired this in: before
the runner existed, sessions were created in the DB but no engine drove
them. UI said "active", nothing traded.
"""
import asyncio
from typing import Dict
from loguru import logger
from sqlalchemy import select

from app.database import async_session_factory
from app.models.trade import TradeSession
from app.models.user import BrokerAccount
from app.models.strategy import Strategy
from app.engines.live_trading.live_trader import LiveTrader
from app.engines.live_trading.broker_factory import build_broker_from_account as get_broker  # was get_broker (renamed)
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig

_active_sessions: Dict[str, asyncio.Task] = {}


async def start_live_session(session_id: str, strategy_id: str, user_id: str,
                              broker_account_id: str, instrument: str = "ES"):
    if session_id in _active_sessions and not _active_sessions[session_id].done():
        logger.info(f"[LiveRunner] session {session_id} already running")
        return
    task = asyncio.create_task(_run_session(
        session_id, strategy_id, user_id, broker_account_id, instrument,
    ))
    _active_sessions[session_id] = task
    return task


async def stop_live_session(session_id: str):
    t = _active_sessions.pop(session_id, None)
    if t and not t.done():
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


_active_live_traders: dict = {}  # ROUTING (#156): session_id:instrument -> LiveTrader


async def _run_session(session_id, strategy_id, user_id, broker_account_id, instrument):
    try:
        async with async_session_factory() as db:
            strat_row = (await db.execute(
                select(Strategy).where(Strategy.id == strategy_id)
            )).scalar_one_or_none()
            acct_row = (await db.execute(
                select(BrokerAccount).where(BrokerAccount.id == broker_account_id)
            )).scalar_one_or_none()
        if not strat_row or not acct_row:
            logger.error(f"[LiveRunner] missing strategy/account for {session_id}")
            return

        cfg = StrategyConfig(
            name=strat_row.name,
            instruments=strat_row.instruments or [instrument],
            primary_timeframe=strat_row.primary_timeframe or "15m",
            execution_timeframe=strat_row.execution_timeframe or "1m",
            higher_timeframes=strat_row.higher_timeframes or ["1h", "4h"],
            risk_reward_ratio=strat_row.risk_reward_ratio or 2.0,
            stop_loss_type=strat_row.stop_loss_type or "structure",
            stop_loss_ticks=strat_row.stop_loss_ticks,
            max_contracts=strat_row.max_contracts or 1,
            session_filters=strat_row.session_filters or [],
        )
        strategy = ICTStrategy(cfg, instrument=instrument)
        broker = get_broker(acct_row)
        await broker.connect()

        trader = LiveTrader(
            strategy=strategy,
            broker=broker,
            instrument=instrument,
            session_id=session_id,
            user_id=user_id,
            strategy_id=strategy_id,
            broker_account_id=broker_account_id,
        )
        trader._is_running = True
        _active_live_traders[f"{session_id}:{instrument}"] = trader
        logger.info(f"[LiveRunner] Started session {session_id} | {instrument}")

        # Main loop — pull bars from broker every minute, push to strategy
        while True:
            try:
                async with async_session_factory() as db:
                    sess = (await db.execute(
                        select(TradeSession).where(TradeSession.id == session_id)
                    )).scalar_one_or_none()
                    if not sess or not sess.is_active:
                        logger.info(f"[LiveRunner] session {session_id} stopped")
                        break
                bars = await broker.fetch_bars(instrument, timeframe="1m", count=60)
                if bars:
                    await trader.on_bar({"1m": bars, cfg.primary_timeframe: bars})
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LiveRunner] loop error: {e}")
                await asyncio.sleep(30)
    except Exception as e:
        logger.error(f"[LiveRunner] fatal: {e}")
        import traceback; traceback.print_exc()
    finally:
        _active_sessions.pop(session_id, None)
