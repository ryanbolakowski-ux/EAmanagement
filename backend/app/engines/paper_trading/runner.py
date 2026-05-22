"""
Paper Trading Background Runner.
Manages active paper trading sessions as asyncio tasks.
"""
import asyncio
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger
from sqlalchemy import select

from app.database import async_session_factory
from app.models.strategy import Strategy
from app.models.trade import TradeSession, Trade, TradingMode
from app.engines.paper_trading.paper_trader import PaperTrader
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.data_feeds.local_cache import fetch_from_cache
from app.engines.strategy_engine.base_strategy import StrategyConfig

YAHOO_SYMBOLS = {"ES": "ES=F", "NQ": "NQ=F", "RTY": "RTY=F", "YM": "YM=F"}

# A session may run on multiple instruments concurrently; each (session, instrument)
# gets its own asyncio task and PaperTrader. They're tracked together under the
# session_id so stop_paper_session can cancel them all.
_active_tasks: dict[str, list[asyncio.Task]] = {}
_active_traders: dict[str, object] = {}


def _task_key(session_id: str, instrument: str) -> str:
    return f"{session_id}:{instrument}"


async def start_paper_session(session_id: str, strategy_id: str, user_id: str, instrument: str = "ES"):
    key = _task_key(session_id, instrument)
    bucket = _active_tasks.setdefault(session_id, [])
    if any(getattr(t, "_edge_key", None) == key and not t.done() for t in bucket):
        logger.warning(f"[PaperRunner] Session {session_id} on {instrument} already running")
        return
    task = asyncio.create_task(_run_paper_loop(session_id, strategy_id, user_id, instrument))
    setattr(task, "_edge_key", key)
    bucket.append(task)
    logger.info(f"[PaperRunner] Started session {session_id} on {instrument}")


async def stop_paper_session(session_id: str):
    """Cancel every task running under this session_id (across all instruments)."""
    tasks = _active_tasks.pop(session_id, [])
    for task in tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    # Drop any associated trader entries
    keys_to_drop = [k for k in _active_traders if k == session_id or k.startswith(session_id + ":")]
    for k in keys_to_drop:
        _active_traders.pop(k, None)
    logger.info(f"[PaperRunner] Stopped session {session_id} ({len(tasks)} task(s))")


def _fetch_bars_sync(instrument, timeframe="1m", count=3):
    """Fetch latest bars from Yahoo Finance (minimal call)."""
    symbol = YAHOO_SYMBOLS.get(instrument.upper(), instrument + "=F")
    # Multi-day period avoids gaps around the daily Globex break (5–6pm ET)
    # and the midnight rollover, so the runner keeps seeing bars overnight.
    period_map = {"1m": "5d", "5m": "5d", "15m": "10d", "30m": "10d",
                  "1h": "30d", "1d": "60d"}
    period = period_map.get(timeframe, "5d")
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=timeframe)
        if df is None or df.empty:
            return []
        df = df.tail(count)
        bars = []
        for ts, row in df.iterrows():
            bars.append({
                "timestamp": ts.to_pydatetime(),
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
                "volume": int(row["Volume"]),
            })
        return bars
    except Exception as e:
        logger.error("[PaperRunner] Fetch error: " + str(e))
        return []


async def _preload_from_cache(instrument, timeframes, trader):
    """Load historical bars from local candle_cache to seed the strategy buffer."""
    from datetime import timezone
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)
    
    for tf in timeframes:
        try:
            df = await fetch_from_cache(
                instrument=instrument,
                start_date=start,
                end_date=end,
                interval=tf,
            )
            if df is not None and not df.empty:
                bars_list = []
                for ts, row in df.tail(400).iterrows():
                    bars_list.append({
                        "timestamp": ts.to_pydatetime(),
                        "open": float(row["open"]),
                        "high": float(row["high"]),
                        "low": float(row["low"]),
                        "close": float(row["close"]),
                        "volume": int(row["volume"]),
                    })
                trader._bars_buffer[tf] = bars_list
                count = len(bars_list)
                logger.info(f"[PaperRunner] Preloaded {count} {tf} bars for {instrument} from cache")
            else:
                logger.warning(f"[PaperRunner] No cached data for {instrument} {tf}")
        except Exception as e:
            logger.error(f"[PaperRunner] Cache preload error {instrument} {tf}: {e}")


async def _run_paper_loop(session_id: str, strategy_id: str, user_id: str, instrument: str):
    """Main loop: fetch bars, feed to strategy, save trades."""
    try:
        async with async_session_factory() as db:
            result = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
            strategy_model = result.scalar_one_or_none()
            if not strategy_model:
                logger.error("[PaperRunner] Strategy " + strategy_id + " not found")
                return
            config = StrategyConfig(
                name=strategy_model.name,
                instruments=strategy_model.instruments or [instrument],
                primary_timeframe=strategy_model.primary_timeframe or "15m",
                execution_timeframe=strategy_model.execution_timeframe or "1m",
                higher_timeframes=strategy_model.higher_timeframes or [],
                risk_reward_ratio=strategy_model.risk_reward_ratio or 2.0,
                stop_loss_type=strategy_model.stop_loss_type or "structure",
                stop_loss_ticks=strategy_model.stop_loss_ticks,
                max_contracts=strategy_model.max_contracts or 1,
                session_filters=strategy_model.session_filters or [],
                fvg_min_size_ticks=strategy_model.fvg_min_size_ticks or 4,
                fvg_max_size_ticks=strategy_model.fvg_max_size_ticks,
                max_daily_loss=strategy_model.max_daily_loss,
                max_trades_per_day=strategy_model.max_trades_per_day,
                use_rsi_filter=bool((strategy_model.rule_tree or {}).get("use_rsi_filter", False)),
                use_vwap_filter=bool((strategy_model.rule_tree or {}).get("use_vwap_filter", False)),
            )
            ict_strategy = ICTStrategy(config, instrument=instrument)
            trader = PaperTrader(
                ict_strategy, instrument=instrument, session_id=session_id,
                user_id=user_id, strategy_id=strategy_id,
            )
            _active_traders[_task_key(session_id, instrument)] = trader
            await trader.start()

        primary_tf = strategy_model.primary_timeframe or "15m"
        exec_tf = strategy_model.execution_timeframe or "1m"
        # Build list of timeframes to fetch
        all_tfs = list(set([primary_tf, exec_tf] + (strategy_model.higher_timeframes or [])))
        # Map to yfinance intervals
        tf_map = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1H": "1h", "1h": "1h", "4H": "1h", "1D": "1d"}
        last_bar_times = {}

        # Preload historical bars from local cache
        await _preload_from_cache(instrument, all_tfs, trader)

        # Seed last_bar_times so we DON'T retroactively fire signals on the
        # 20–50 already-old bars that come in on the first poll. Without this,
        # the strategy would replay recent history and almost always open a
        # trade at activation based on a setup from 5–30 minutes ago. Only
        # genuinely new bars (printed after this point) should trigger signals.
        from datetime import timezone as _tz
        _now = datetime.now(_tz.utc)
        for _tf in all_tfs:
            buf = trader._bars_buffer.get(_tf) or []
            if buf:
                last_bar_times[_tf] = buf[-1]["timestamp"]
            else:
                last_bar_times[_tf] = _now - timedelta(minutes=1)

        # Sync session net_pnl from trades table (in case of restart)
        try:
            async with async_session_factory() as sync_db:
                from sqlalchemy import text as sync_text
                r = await sync_db.execute(sync_text(
                    "SELECT COUNT(*), COALESCE(SUM(pnl), 0) FROM trades WHERE session_id = :sid"
                ), {"sid": session_id})
                row = r.fetchone()
                if row and row[0] > 0:
                    await sync_db.execute(sync_text(
                        "UPDATE trade_sessions SET total_trades = :tc, net_pnl = :pnl WHERE id = :sid"
                    ), {"tc": row[0], "pnl": float(row[1]), "sid": session_id})
                    await sync_db.commit()
                    logger.info(f"[PaperRunner] Synced session {session_id}: {row[0]} trades, PnL")
                    # Push current equity into the trader so risk-based
                    # contract sizing starts from real account state, not
                    # the default $10k seed.
                    try:
                        trader._equity = trader._starting_balance + float(row[1] or 0.0)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"[PaperRunner] Session sync failed: {e}")

        logger.info("[PaperRunner] Polling " + instrument + " every 60s | TFs: " + str(all_tfs))

        first_iteration = True

        while True:
            try:
                # On the very first poll we may be feeding hours of bars (the
                # gap between the cache's last bar and Yahoo's latest). Put
                # the trader in warmup so those bars seed the buffer without
                # firing entries on stale setups.
                if first_iteration:
                    trader._warmup = True

                # Fetch bars for each timeframe in a thread
                for tf in all_tfs:
                    yf_tf = tf_map.get(tf, "15m")
                    bars = await asyncio.to_thread(_fetch_bars_sync, instrument, yf_tf, 50)
                    for bar in bars:
                        bar_time = bar["timestamp"]
                        last_t = last_bar_times.get(tf)
                        if last_t and bar_time <= last_t:
                            continue
                        last_bar_times[tf] = bar_time
                        await trader.on_bar(tf, bar)

                # Warmup ends after the first iteration. From here on, every
                # `on_bar` call is for a genuinely fresh bar and signals fire
                # normally.
                if first_iteration:
                    trader._warmup = False
                    first_iteration = False
                    logger.info(f"[PaperRunner] {instrument} warmup complete — live signals enabled")

                # Save any new completed trades to DB
                await _save_new_trades(trader, session_id, strategy_id, user_id, instrument)
                # Update session stats in DB
                await _update_session_stats(session_id, trader)

                # Check if session is still active
                still_active = await _check_session_active(session_id)
                if not still_active:
                    await trader.stop()
                    logger.info("[PaperRunner] Session " + session_id + " deactivated")
                    break

            except Exception as e:
                logger.error("[PaperRunner] Loop error: " + str(e))
                import traceback
                traceback.print_exc()

            await asyncio.sleep(60)

    except asyncio.CancelledError:
        logger.info("[PaperRunner] Session " + session_id + " cancelled")
    except Exception as e:
        logger.error("[PaperRunner] Session " + session_id + " error: " + str(e))
        import traceback
        traceback.print_exc()
    finally:
        # Remove just this (session, instrument) task — leave any sibling tasks
        # under the same session_id alone.
        bucket = _active_tasks.get(session_id, [])
        bucket[:] = [t for t in bucket if getattr(t, "_edge_key", None) != _task_key(session_id, instrument)]
        if not bucket:
            _active_tasks.pop(session_id, None)
        _active_traders.pop(_task_key(session_id, instrument), None)


async def _save_new_trades(trader, session_id, strategy_id, user_id, instrument):
    trades = trader._completed_trades
    if not trades:
        return
    # Bug #10 fix: persistence high-water mark on the trader itself, NOT
    # COUNT(trades)-then-slice. After restart the trader has empty
    # _completed_trades but DB holds N rows; old code computed [][N:] = []
    # forever, dropping every newly closed trade.
    if not hasattr(trader, "_persisted_count"):
        async with async_session_factory() as _db0:
            _row = await _db0.execute(select(Trade).where(Trade.session_id == session_id))
            trader._persisted_count = len(_row.scalars().all())
    existing_count = trader._persisted_count
    new_trades = trades[existing_count:]
    if not new_trades:
        return
    async with async_session_factory() as db:
        for t in new_trades:
            # `notes` carries the chart context the strategy captured at
            # signal time (chart_candles + chart_fvgs + bias + fvg_type +
            # primary_tf). Without this, the View-Trade modal shows nothing.
            md = t.metadata or {}
            trade = Trade(
                strategy_id=strategy_id,
                user_id=user_id,
                session_id=session_id,
                mode="paper",
                status="closed",
                instrument=instrument,
                direction=t.direction,
                contracts=t.contracts,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                stop_loss=md.get("stop_loss") or md.get("sl") or 0,
                take_profit=md.get("take_profit") or md.get("tp") or 0,
                entry_time=t.entry_time,
                exit_time=t.exit_time,
                pnl=t.pnl,
                commission=t.commission,
                net_pnl=t.net_pnl,
                exit_reason=t.exit_reason,
                notes=md,
            )
            db.add(trade)
        if new_trades:
            await db.commit()
            # Bug #10 fix: advance persistence mark only after successful commit
            trader._persisted_count = existing_count + len(new_trades)
            logger.info("[PaperRunner] Saved " + str(len(new_trades)) + " new trades")


async def _update_session_stats(session_id, trader):
    stats = trader.stats
    async with async_session_factory() as db:
        result = await db.execute(select(TradeSession).where(TradeSession.id == session_id))
        session = result.scalar_one_or_none()
        if session:
            session.total_trades = stats["total_trades"]
            session.net_pnl = stats["net_pnl"]
            await db.commit()


async def _check_session_active(session_id):
    async with async_session_factory() as db:
        result = await db.execute(select(TradeSession).where(TradeSession.id == session_id))
        session = result.scalar_one_or_none()
        return session.is_active if session else False


def get_open_positions():
    """Return open positions from all active paper traders."""
    positions = []
    for session_id, trader in _active_traders.items():
        if hasattr(trader, '_position') and trader._position:
            p = trader._position
            last_price = getattr(trader, '_last_price', 0.0)
            tick_size = 0.25 if p.instrument in ('ES', 'NQ', 'YM') else 0.10
            tick_value = 12.50 if p.instrument in ('ES', 'NQ') else 5.0 if p.instrument == 'YM' else 5.0
            if p.direction == 'long':
                unrealized = ((last_price - p.entry_price) / tick_size) * tick_value * p.contracts
            else:
                unrealized = ((p.entry_price - last_price) / tick_size) * tick_value * p.contracts
            positions.append({
                'session_id': session_id,
                'instrument': p.instrument,
                'direction': p.direction,
                'entry_price': p.entry_price,
                'stop_loss': p.stop_loss,
                'take_profit': p.take_profit,
                'contracts': p.contracts,
                'entry_time': p.entry_time.isoformat() if p.entry_time else None,
                'current_price': last_price,
                'unrealized_pnl': round(unrealized, 2),
                'status': 'open',
            })
    return positions
