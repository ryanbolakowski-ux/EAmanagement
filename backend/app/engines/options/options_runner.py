"""Options paper-trading runner.

One asyncio task per active session × underlying. On each tick:
  1. Pull the latest bars for the underlying from Polygon stock aggs.
  2. Pull (and cache for 6h) the full options chain in the configured DTE band.
  3. Feed the strategy's ICTStrategy logic on the underlying's bars.
  4. If a signal fires, route through `OptionsPaperTrader.on_signal()` which
     picks the strike and opens the position.
  5. Mark-to-model every cycle via `on_spot_tick()`.
  6. Persist any closed positions to the `trades` table.

Why we re-use the futures ICTStrategy: the user's options strategies are
fundamentally directional setups on the underlying (Trend Pullback, Breakout,
etc.). The `options_mode` field decides *how* to translate that directional
signal into an option contract — not whether to take the signal. So the
signal logic stays identical to futures; only the position-opening path
diverges.
"""
import asyncio
import uuid
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from loguru import logger

import pandas as pd
from sqlalchemy import select, text

from app.database import async_session_factory
from app.models.strategy import Strategy
from app.models.trade import TradeSession, Trade, TradingMode, TradeStatus
from app.engines.options.polygon_options import PolygonOptionsClient, OptionContract
from app.engines.options.polygon_throttle import gate as _poly_gate
from app.engines.options.options_paper import OptionsPaperTrader
from app.engines.options.pricing import price as bs_price
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig, SignalType
from app.engines.data_feeds.polygon_feed import POLYGON_API_KEY


# Active runners — keyed by (session_id, underlying)
_active: dict[tuple[str, str], asyncio.Task] = {}

# Per-runner chain cache so we don't pound the Polygon contracts endpoint.
# Refresh once every 6h — chains barely change intraday for the DTE window
# we care about. Key: (underlying, side).
_chain_cache: dict[tuple[str, str], tuple[datetime, list[OptionContract]]] = {}
_CHAIN_TTL = timedelta(hours=6)

# Cache the polygon-anchored "today" — refresh once an hour
_polygon_today_cache: dict = {"date": None, "fetched_at": None}


async def _polygon_today() -> date:
    """Return the most recent trading date Polygon has SPY data for.

    The container clock can drift from the real market (synthetic 2026 in
    test environments). Polygon serves real-world data so we anchor all
    options/chain queries to whatever Polygon considers "today".
    """
    from datetime import datetime as _dt
    cache = _polygon_today_cache
    now = _dt.now(timezone.utc)
    if cache.get("date") and cache.get("fetched_at") and (now - cache["fetched_at"]).total_seconds() < 3600:
        return cache["date"]
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://api.polygon.io/v2/aggs/ticker/SPY/prev",
                params={"adjusted": "true", "apiKey": POLYGON_API_KEY},
            )
            r.raise_for_status()
            results = (r.json() or {}).get("results") or []
            if results:
                ts_ms = results[0]["t"]
                d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
                cache["date"] = d
                cache["fetched_at"] = now
                return d
    except Exception as e:
        logger.warning(f"[OptionsRunner] _polygon_today probe failed: {e}")
    # Fallback to real wall-clock UTC
    return date.today()




async def _fetch_underlying_bars(underlying: str, lookback_days: int = 5,
                                  interval: str = "1m") -> pd.DataFrame:
    """Pull recent bars for the stock underlying from Polygon's stock aggs.
    Returns DataFrame indexed by timestamp with open/high/low/close/volume."""
    import httpx
    # Use polygon's anchor date (real market time) rather than the
    # container's potentially-synthetic clock — otherwise we query future
    # dates that Polygon has no data for yet.
    poly_today = await _polygon_today()
    end = datetime.combine(poly_today, datetime.min.time(), tzinfo=timezone.utc)
    start = end - timedelta(days=lookback_days)
    end = end + timedelta(days=1)  # include today's session
    timespan_map = {"1m": ("minute", 1), "5m": ("minute", 5),
                    "15m": ("minute", 15), "1H": ("hour", 1),
                    "1h": ("hour", 1), "1D": ("day", 1)}
    timespan, mult = timespan_map.get(interval, ("minute", 1))
    url = (f"https://api.polygon.io/v2/aggs/ticker/{underlying.upper()}"
            f"/range/{mult}/{timespan}/{start.date()}/{end.date()}"
            f"?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}")
    try:
        await _poly_gate.acquire()
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url)
            r.raise_for_status()
            results = (r.json() or {}).get("results", [])
    except Exception as e:
        logger.error(f"[OptionsRunner] underlying bars fetch failed for {underlying}: {e}")
        return pd.DataFrame()
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                              "c": "close", "v": "volume"})
    return df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]


async def _get_chain(underlying: str, side: str,
                      dte_min: int, dte_max: int) -> list[OptionContract]:
    """Return a (cached) options chain for underlying×side in the DTE window."""
    key = (underlying.upper(), side)
    now = datetime.now(timezone.utc)
    hit = _chain_cache.get(key)
    if hit and (now - hit[0]) < _CHAIN_TTL:
        return hit[1]

    client = PolygonOptionsClient()
    today = date.today()
    try:
        contracts = await client.list_contracts(
            underlying=underlying, right=side,
            expiration_after=today + timedelta(days=dte_min),
            expiration_before=today + timedelta(days=dte_max),
            limit=250,
        )
        _chain_cache[key] = (now, contracts)
        return contracts
    except Exception as e:
        logger.error(f"[OptionsRunner] chain fetch failed for {underlying}/{side}: {e}")
        # If we have a stale cache, prefer that over nothing
        return hit[1] if hit else []


def _build_strategy_config(strategy_model: Strategy, underlying: str) -> StrategyConfig:
    return StrategyConfig(
        name=strategy_model.name,
        instruments=[underlying],
        primary_timeframe=strategy_model.primary_timeframe or "5m",
        execution_timeframe=strategy_model.execution_timeframe or "1m",
        higher_timeframes=strategy_model.higher_timeframes or ["1H"],
        risk_reward_ratio=strategy_model.risk_reward_ratio or 2.0,
        stop_loss_type=strategy_model.stop_loss_type or "structure",
        stop_loss_ticks=strategy_model.stop_loss_ticks,
        max_contracts=strategy_model.max_contracts or 1,
        session_filters=strategy_model.session_filters or [],
        fvg_min_size_ticks=strategy_model.fvg_min_size_ticks or 4,
        fvg_max_size_ticks=strategy_model.fvg_max_size_ticks,
        max_daily_loss=strategy_model.max_daily_loss,
        max_trades_per_day=strategy_model.max_trades_per_day,
    )


async def _persist_close(session_id: str, strategy_id: str, user_id: str,
                          result, position) -> None:
    """Write a closed options trade to the trades table."""
    try:
        async with async_session_factory() as db:
            t = Trade(
                strategy_id=strategy_id,
                user_id=user_id,
                session_id=session_id,
                mode=TradingMode.PAPER,
                status=TradeStatus.CLOSED,
                instrument=result.contract_ticker,
                direction=result.direction,
                contracts=result.contracts,
                entry_price=result.entry_premium,
                exit_price=result.exit_premium,
                stop_loss=position.stop_premium,
                take_profit=position.target_premium,
                entry_time=result.entry_time,
                exit_time=result.exit_time,
                pnl=result.gross_pnl,
                commission=result.commission,
                net_pnl=result.net_pnl,
                exit_reason=result.exit_reason,
                notes={
                    "strike": position.contract.strike,
                    "expiration": position.contract.expiration.isoformat(),
                    "right": position.contract.right,
                    "entry_spot": position.entry_spot,
                    "iv_used": position.estimated_iv,
                    **position.metadata,
                },
            )
            db.add(t)
            await db.commit()
    except Exception as e:
        logger.error(f"[OptionsRunner] persist failed: {e}")


async def _run_session_loop(session_id: str, strategy_id: str, user_id: str,
                              underlying: str, starting_balance: float):
    """The main loop for one (session, underlying)."""
    logger.info(f"[OptionsRunner] Loop start | session={session_id} | underlying={underlying}")

    # Load strategy
    async with async_session_factory() as db:
        s_res = await db.execute(select(Strategy).where(Strategy.id == strategy_id))
        strategy_model = s_res.scalar_one_or_none()
        if not strategy_model:
            logger.error(f"[OptionsRunner] strategy {strategy_id} not found")
            return

    cfg = _build_strategy_config(strategy_model, underlying)
    ict = ICTStrategy(cfg, instrument=underlying)

    # Determine call/put bias from strategy config — Trend Pullback uses the
    # rule_tree.bias hint; Breakout & default is bullish; Wheel is bullish
    # (we sell puts to acquire). We allow ICTStrategy to override per-signal.
    default_side_hint = "call"

    # Initial chain pull
    dte_min = int(getattr(strategy_model, "options_min_dte", 30) or 30)
    dte_max = int(getattr(strategy_model, "options_max_dte", 60) or 60)
    delta_min = float(getattr(strategy_model, "options_target_delta_min", 0.30) or 0.30)
    delta_max = float(getattr(strategy_model, "options_target_delta_max", 0.50) or 0.50)
    prefer_itm = bool(getattr(strategy_model, "options_prefer_itm", False))
    spread_width = (int(getattr(strategy_model, "options_spread_width", 0) or 0)
                     if getattr(strategy_model, "options_mode", "") == "vertical_spread"
                     else None)
    risk_pct = float(getattr(strategy_model, "options_risk_per_trade_pct", 1.5) or 1.5)

    # Initial chain (calls)
    chain_calls = await _get_chain(underlying, "call", dte_min, dte_max)
    chain_puts  = await _get_chain(underlying, "put",  dte_min, dte_max)

    if not chain_calls and not chain_puts:
        logger.error(f"[OptionsRunner] empty chain for {underlying} — aborting session")
        return

    trader = OptionsPaperTrader(
        underlying=underlying,
        chain=chain_calls + chain_puts,
        starting_balance=starting_balance,
        risk_per_trade_pct=risk_pct,
        session_id=session_id, user_id=user_id, strategy_id=strategy_id,
    )
    trader.start()

    last_bar_ts: Optional[pd.Timestamp] = None
    try:
        while True:
            try:
                bars = await _fetch_underlying_bars(underlying, lookback_days=2, interval="1m")
                if bars.empty:
                    await asyncio.sleep(60)
                    continue

                # Run strategy when a new bar prints
                latest_ts = bars.index[-1]
                if last_bar_ts is None or latest_ts > last_bar_ts:
                    last_bar_ts = latest_ts

                    # Build the multi-TF dict ICTStrategy expects
                    bars_dict = {cfg.primary_timeframe: bars,
                                  cfg.execution_timeframe: bars}
                    # Add a 1H resample for the HTF bias
                    if "1H" in cfg.higher_timeframes:
                        bars_dict["1H"] = bars.resample("1h").agg({
                            "open": "first", "high": "max", "low": "min",
                            "close": "last", "volume": "sum",
                        }).dropna()

                    if not trader._position:
                        signal = ict.on_bar(bars_dict)
                        if signal and signal.signal != SignalType.NONE:
                            # Earnings filter — skip when avoid window is set & not earnings_catalyst mode
                            from app.engines.options.earnings_filter import is_near_earnings
                            avoid_d = int(getattr(strategy_model, "options_avoid_earnings_days", 0) or 0)
                            _mode = getattr(strategy_model, "options_mode", "") or ""
                            if avoid_d > 0 and _mode != "earnings_catalyst":
                                _near, _ed = await is_near_earnings(underlying, date.today(), avoid_d)
                                if _near:
                                    logger.info(f"[OptionsRunner] SKIP — {underlying} earnings {_ed} within {avoid_d}d")
                                    continue
                            spot = float(bars["close"].iloc[-1])
                            side = "long" if signal.signal == SignalType.LONG else "short"
                            poly_today_local = await _polygon_today()
                            trader.on_signal(
                                side=side, spot=spot, today=poly_today_local,
                                delta_band=(delta_min, delta_max),
                                dte_band=(dte_min, dte_max),
                                prefer_itm=prefer_itm,
                                spread_width=spread_width,
                                default_iv=0.30,
                            )

                # Mark-to-model — every cycle
                if trader._position:
                    spot = float(bars["close"].iloc[-1])
                    closed = trader.on_spot_tick(spot=spot, now=datetime.now(timezone.utc))
                    if closed:
                        # Need the position state at the time we closed —
                        # we already cleared it but kept the result. Persist
                        # via the result + a synthesised position copy.
                        # For now we capture the last completed trade.
                        last = trader._completed[-1] if trader._completed else None
                        if last:
                            # Bug #14 fix: build a small namespace with the REAL
                            # stop/target from last.metadata. Was a placeholder class
                            # that wrote fake 0.5x/2x values onto every options trade.
                            _md = (getattr(last, "metadata", None) or {})
                            _pos = type("_P", (), {
                                "stop_premium":   _md.get("stop_premium")   or _md.get("stop"),
                                "target_premium": _md.get("target_premium") or _md.get("take_profit") or _md.get("target"),
                                "entry_spot":     getattr(last, "entry_spot", None),
                                "estimated_iv":   0.30,
                                "metadata":       getattr(last, "metadata", {}) or {},
                                "contract":       type("C", (), {
                                    "strike":     0.0,
                                    "expiration": date.today(),
                                    "right":      getattr(last, "direction", ""),
                                })(),
                            })()
                            await _persist_close(session_id, strategy_id, user_id, last, _pos)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[OptionsRunner] loop tick error: {e}")

            await asyncio.sleep(60)  # poll cadence

    except asyncio.CancelledError:
        logger.info(f"[OptionsRunner] Cancelled | session={session_id} | underlying={underlying}")
    finally:
        trader.stop()


async def start_options_session(session_id: str, strategy_id: str, user_id: str,
                                  underlyings: list[str],
                                  starting_balance: float = 10_000.0):
    """Spawn one runner per underlying for this session."""
    for u in underlyings:
        key = (session_id, u.upper())
        if key in _active and not _active[key].done():
            continue
        task = asyncio.create_task(
            _run_session_loop(session_id, strategy_id, user_id, u.upper(), starting_balance)
        )
        _active[key] = task
        logger.info(f"[OptionsRunner] Spawned task for {u}")


async def stop_options_session(session_id: str):
    """Cancel all runner tasks for this session."""
    keys = [k for k in _active.keys() if k[0] == session_id]
    for k in keys:
        task = _active.pop(k, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    logger.info(f"[OptionsRunner] Stopped session {session_id} ({len(keys)} runners)")
