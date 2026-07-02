"""Live options paper/sandbox/production runner.

Mirrors `options_runner.py` (paper) but plugs an OptionsLiveTrader into a
real TradierBroker. Each session × underlying gets its own asyncio task.

Tradier supplies the chain (not Polygon) because it returns greeks/IV/quotes
inline, which our paper engine had to synthesize via Black-Scholes. So a
live session has an end-to-end real-pricing path:
    underlying bars (Polygon stock aggs) → ICT signal → Tradier chain →
    strike picker → Tradier quote → place order → Tradier fills.
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
from app.models.trade import Trade, TradeSession, TradingMode, TradeStatus
from app.engines.options.options_live import OptionsLiveTrader
from app.engines.options.polygon_options import OptionContract
from app.engines.options.options_runner import _fetch_underlying_bars, _polygon_today
from app.engines.live_trading.broker_factory import build_broker_from_account
from app.engines.live_trading.tradier import TradierBroker
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig, SignalType


# Active runners — keyed by (session_id, underlying)
_active: dict[tuple[str, str], asyncio.Task] = {}

# Per-(broker_account_id, underlying, expiration) chain cache.
# TTLCache (was a bare dict): entries for stopped sessions / rotated dte
# bands were never pruned (TTL only checked on read). maxsize=128 bounds it;
# the manual _CHAIN_TTL freshness check below is unchanged.
from app.core.ttl_cache import TTLCache
_chain_cache: TTLCache = TTLCache(maxsize=128, ttl_seconds=6 * 3600)
_CHAIN_TTL = timedelta(hours=6)


async def _tradier_chain(broker: TradierBroker, underlying: str,
                          dte_min: int, dte_max: int,
                          side: str) -> list[OptionContract]:
    """Pull (and cache) the relevant slice of Tradier's chain — all
    expirations inside [dte_min, dte_max] for `underlying`, filtered to
    `side` (call|put). Returns the OptionContract list shape the picker
    expects."""
    today_iso = (await _polygon_today()).isoformat()
    cache_key = (broker.account_id or "anon", underlying.upper(), f"{side}:{dte_min}-{dte_max}")
    now = datetime.now(timezone.utc)
    hit = _chain_cache.get(cache_key)
    if hit and (now - hit[0]) < _CHAIN_TTL:
        return hit[1]

    exps = await broker.get_option_expirations(underlying)
    today = await _polygon_today()
    want_exps = []
    for e in exps:
        try:
            d = date.fromisoformat(e)
            dte = (d - today).days
            if dte_min <= dte <= dte_max:
                want_exps.append(e)
        except Exception:
            continue

    out: list[OptionContract] = []
    for exp in want_exps:
        rows = await broker.get_option_chain(underlying, expiration=exp, include_greeks=False)
        for r in rows:
            kind = (r.get("option_type") or "").lower()
            if kind not in (side,):
                continue
            sym = r.get("symbol", "")
            if not sym:
                continue
            out.append(OptionContract(
                ticker=f"O:{sym}" if not sym.startswith("O:") else sym,
                underlying=underlying.upper(),
                expiration=date.fromisoformat(exp),
                strike=float(r.get("strike", 0.0)),
                right=side,
            ))
    _chain_cache[cache_key] = (now, out)
    return out


async def _persist_close(session_id: str, strategy_id: str, user_id: str,
                          broker_account_id: str, result, position) -> None:
    """Write a closed live options trade to the trades table."""
    try:
        async with async_session_factory() as db:
            t = Trade(
                strategy_id=strategy_id, user_id=user_id,
                broker_account_id=broker_account_id, session_id=session_id,
                mode=TradingMode.LIVE, status=TradeStatus.CLOSED,
                instrument=result.contract_ticker,
                direction=result.direction, contracts=result.contracts,
                entry_price=result.entry_premium, exit_price=result.exit_premium,
                stop_loss=position.stop_premium, take_profit=position.target_premium,
                entry_time=result.entry_time, exit_time=result.exit_time,
                pnl=result.gross_pnl, commission=result.commission,
                net_pnl=result.net_pnl, exit_reason=result.exit_reason,
                broker_order_id=position.open_order_id,
                notes={
                    "strike":     position.contract.strike,
                    "expiration": position.contract.expiration.isoformat(),
                    "right":      position.contract.right,
                    "entry_spot": position.entry_spot,
                    **position.metadata,
                },
            )
            db.add(t)
            await db.commit()
    except Exception as e:
        logger.error(f"[OptionsLiveRunner] persist failed: {e}")


async def _run_session_loop(session_id: str, strategy_id: str, user_id: str,
                              broker_account_id: str, underlying: str,
                              starting_balance: float):
    logger.info(f"[OptionsLiveRunner] start | session={session_id} | underlying={underlying} | "
                 f"account={broker_account_id}")

    # Load strategy + broker account
    async with async_session_factory() as db:
        strat = (await db.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one_or_none()
        acct  = (await db.execute(select(BrokerAccount).where(BrokerAccount.id == broker_account_id))).scalar_one_or_none()
        if not strat or not acct:
            logger.error(f"[OptionsLiveRunner] strategy or broker account missing")
            return
        if (acct.broker or "").lower() != "tradier":
            logger.error(f"[OptionsLiveRunner] broker {acct.broker} does not support options — refusing to start")
            return

    broker = build_broker_from_account(acct)
    if broker is None:
        logger.error(f"[OptionsLiveRunner] could not build broker adapter")
        return

    cfg = StrategyConfig(
        name=strat.name, instruments=[underlying],
        primary_timeframe=strat.primary_timeframe or "5m",
        execution_timeframe=strat.execution_timeframe or "1m",
        higher_timeframes=strat.higher_timeframes or ["1H"],
        risk_reward_ratio=strat.risk_reward_ratio or 2.0,
        stop_loss_type=strat.stop_loss_type or "structure",
        max_contracts=strat.max_contracts or 1,
        fvg_min_size_ticks=strat.fvg_min_size_ticks or 4,
    )
    ict = ICTStrategy(cfg, instrument=underlying)

    dte_min = int(getattr(strat, "options_min_dte", 30) or 30)
    dte_max = int(getattr(strat, "options_max_dte", 60) or 60)
    delta_min = float(getattr(strat, "options_target_delta_min", 0.30) or 0.30)
    delta_max = float(getattr(strat, "options_target_delta_max", 0.50) or 0.50)
    prefer_itm = bool(getattr(strat, "options_prefer_itm", False))
    spread_width = (int(getattr(strat, "options_spread_width", 0) or 0)
                     if getattr(strat, "options_mode", "") == "vertical_spread" else None)
    risk_pct = float(getattr(strat, "options_risk_per_trade_pct", 1.5) or 1.5)

    # Open broker connection — must succeed before we even pull a chain
    try:
        ok = await broker.connect()
        if not ok:
            logger.error(f"[OptionsLiveRunner] broker.connect() failed — aborting session")
            return
    except Exception as e:
        logger.error(f"[OptionsLiveRunner] broker connect raised: {e}")
        return

    # Initial chain pull (calls + puts)
    chain_calls = await _tradier_chain(broker, underlying, dte_min, dte_max, "call")
    chain_puts  = await _tradier_chain(broker, underlying, dte_min, dte_max, "put")
    if not chain_calls and not chain_puts:
        logger.error(f"[OptionsLiveRunner] empty Tradier chain for {underlying} — aborting")
        await broker.disconnect()
        return

    trader = OptionsLiveTrader(
        underlying=underlying, broker=broker,
        chain=chain_calls + chain_puts,
        starting_balance=starting_balance,
        risk_per_trade_pct=risk_pct,
        session_id=session_id, user_id=user_id, strategy_id=strategy_id,
    )
    await trader.start()

    # Subscribe to the underlying via Tradier WS — gives us tick-by-tick
    # spot updates instead of waiting for the 1m REST poll.
    _latest_ws_spot = {"price": None, "ts": None}
    async def _on_ws_tick(tick):
        try:
            _latest_ws_spot["price"] = float(tick.get("price")) if tick.get("price") else None
            _latest_ws_spot["ts"]    = tick.get("timestamp")
        except Exception:
            pass
    try:
        await broker.subscribe_quotes(underlying, _on_ws_tick)
        logger.info(f"[OptionsLiveRunner] WS subscribed to {underlying}")
    except Exception as e:
        logger.warning(f"[OptionsLiveRunner] WS subscribe failed (continuing with REST polling): {e}")

    last_bar_ts: Optional[pd.Timestamp] = None
    try:
        while True:
            try:
                bars = await _fetch_underlying_bars(underlying, lookback_days=2, interval="1m")
                if bars.empty:
                    await asyncio.sleep(60)
                    continue

                latest_ts = bars.index[-1]
                if last_bar_ts is None or latest_ts > last_bar_ts:
                    last_bar_ts = latest_ts

                    bars_dict = {cfg.primary_timeframe: bars, cfg.execution_timeframe: bars}
                    if "1H" in cfg.higher_timeframes:
                        bars_dict["1H"] = bars.resample("1h").agg({
                            "open": "first", "high": "max", "low": "min",
                            "close": "last", "volume": "sum",
                        }).dropna()

                    if not trader._position:
                        signal = ict.on_bar(bars_dict)
                        if signal and signal.signal != SignalType.NONE:
                            from app.engines.options.earnings_filter import is_near_earnings
                            avoid_d = int(getattr(strat, "options_avoid_earnings_days", 0) or 0)
                            _mode = getattr(strat, "options_mode", "") or ""
                            if avoid_d > 0 and _mode != "earnings_catalyst":
                                _near, _ed = await is_near_earnings(underlying, await _polygon_today(), avoid_d)
                                if _near:
                                    logger.info(f"[OptionsLiveRunner] SKIP — {underlying} earnings {_ed} within {avoid_d}d")
                                    continue
                            spot = float(bars["close"].iloc[-1])
                            side = "long" if signal.signal == SignalType.LONG else "short"
                            await trader.on_signal(
                                side=side, spot=spot, today=await _polygon_today(),
                                delta_band=(delta_min, delta_max),
                                dte_band=(dte_min, dte_max),
                                prefer_itm=prefer_itm,
                                spread_width=spread_width,
                                bar_ts=latest_ts,
                            )

                if trader._position:
                    # Prefer the WS spot when available — it's seconds-fresh
                    # vs the 1m REST bar
                    ws_price = _latest_ws_spot.get("price")
                    spot = float(ws_price) if ws_price else float(bars["close"].iloc[-1])
                    closed = await trader.on_spot_tick(spot=spot, now=datetime.now(timezone.utc))
                    if closed and trader._completed:
                        # Recreate a position snapshot for persistence
                        last_result = trader._completed[-1]
                        class _P:
                            stop_premium  = last_result.entry_premium * (1 - trader._stop_loss_pct / 100.0)
                            target_premium = last_result.entry_premium * (1 + trader._target_pct / 100.0)
                            entry_spot   = last_result.entry_spot
                            metadata     = last_result.metadata
                            open_order_id = last_result.metadata.get("close_order_id", "")
                            contract     = type("C", (), {
                                "strike":     0.0,
                                "expiration": date.today(),
                                "right":      last_result.direction,
                            })()
                        await _persist_close(session_id, strategy_id, user_id,
                                              broker_account_id, last_result, _P())

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[OptionsLiveRunner] tick error: {e}")

            await asyncio.sleep(60)

    except asyncio.CancelledError:
        logger.info(f"[OptionsLiveRunner] cancelled | session={session_id}")
    finally:
        await trader.stop()
        try:
            await broker.disconnect()
        except Exception:
            pass


async def start_live_options_session(session_id: str, strategy_id: str, user_id: str,
                                       broker_account_id: str, underlyings: list[str],
                                       starting_balance: float = 10_000.0):
    for u in underlyings:
        key = (session_id, u.upper())
        if key in _active and not _active[key].done():
            continue
        task = asyncio.create_task(_run_session_loop(
            session_id, strategy_id, user_id, broker_account_id, u.upper(), starting_balance
        ))
        _active[key] = task
        logger.info(f"[OptionsLiveRunner] spawned task for {u}")


async def stop_live_options_session(session_id: str):
    keys = [k for k in _active.keys() if k[0] == session_id]
    for k in keys:
        task = _active.pop(k, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    logger.info(f"[OptionsLiveRunner] stopped session {session_id} ({len(keys)} runners)")
