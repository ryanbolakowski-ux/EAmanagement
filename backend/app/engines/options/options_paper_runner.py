"""Options paper trading runner — drives an OptionsPaperTrader on live
underlying quotes from yfinance. Mirrors the futures paper runner pattern
so the user gets the same dashboard experience.

Architecture per session (user x strategy x underlying):
  1. Build an OptionsPaperTrader (handles BS pricing + position state)
  2. Loop every 60s during market hours:
     - Pull underlying spot (yfinance 1m bars, 15-min delayed but fine for paper)
     - Feed to the strategy's signal generator
     - If signal: trader.on_signal() picks strike + opens position
     - If open: trader.mark_to_market() recomputes premium, checks SL/TP/expiry
  3. On close: write to trades table with mode='options_paper'
"""
import asyncio
from datetime import datetime, date, timezone, timedelta
from typing import Dict
from loguru import logger

from sqlalchemy import select, text
import yfinance as yf

from app.database import async_session_factory
from app.models.trade import TradeSession, TradingMode, Trade
from app.models.strategy import Strategy
from app.engines.options.options_paper import OptionsPaperTrader
from app.engines.options.pricing import price as bs_price
from app.engines.options.polygon_options import OptionContract


_active: Dict[str, asyncio.Task] = {}


async def start_options_paper_session(session_id: str, strategy_id: str, user_id: str,
                                        underlying: str, watchlist: list[str] | None = None):
    if session_id in _active and not _active[session_id].done():
        logger.info(f"[OptionsPaperRunner] session {session_id} already running")
        return _active[session_id]
    t = asyncio.create_task(_run(session_id, strategy_id, user_id, underlying.upper(), watchlist or [underlying.upper()]))
    _active[session_id] = t
    return t


async def stop_options_paper_session(session_id: str):
    t = _active.pop(session_id, None)
    if t and not t.done():
        t.cancel()
        try: await t
        except asyncio.CancelledError: pass


def _fetch_spot(ticker: str) -> float | None:
    """Latest spot from yfinance (15-min delayed during market hours)."""
    try:
        df = yf.Ticker(ticker).history(period="1d", interval="1m")
        if df is None or df.empty:
            return None
        return float(df.iloc[-1]["Close"])
    except Exception as e:
        logger.warning(f"[OptionsPaperRunner] spot fetch failed for {ticker}: {e}")
        return None


def _build_synthetic_chain(underlying: str, spot: float, iv: float = 0.30,
                             dte_min: int = 14, dte_max: int = 60,
                             strike_spread_pct: float = 0.10) -> list[OptionContract]:
    """Build a synthetic chain centred on spot. We don't need to fetch a real
    chain — the strike picker just needs candidate contracts and the trader
    will price them with Black-Scholes. We generate strikes ±10% in $1 steps
    and expiries at 14, 30, 45, 60 DTE."""
    chain: list[OptionContract] = []
    today = date.today()
    expiries = [today + timedelta(days=d) for d in (14, 21, 30, 45, 60)]
    expiries = [e for e in expiries if dte_min <= (e - today).days <= dte_max]
    if not expiries:
        expiries = [today + timedelta(days=30)]

    # Strike step: $1 for low-priced, $5 for high-priced
    step = 1 if spot < 100 else 5
    low  = round(spot * (1 - strike_spread_pct))
    high = round(spot * (1 + strike_spread_pct))
    strikes = list(range(low, high + step, step))

    for exp in expiries:
        for k in strikes:
            for opt_type in ("call", "put"):
                chain.append(OptionContract(
                    underlying=underlying, expiration=exp,
                    strike=float(k), option_type=opt_type,
                    symbol=f"{underlying}{exp.strftime('%y%m%d')}{opt_type[0].upper()}{int(k*1000):08d}",
                ))
    return chain


def _ema_signal(closes: list[float]) -> str | None:
    """Tiny signal stub: 9 EMA crosses 21 EMA → long; reverse → short.
    Real strategies plug in via the strategy_id but this is the default for
    Trend Pullback / Breakout / Earnings strategies in paper mode."""
    if len(closes) < 25:
        return None
    def ema(values, period):
        k = 2 / (period + 1)
        e = values[0]
        for v in values[1:]:
            e = v * k + e * (1 - k)
        return e
    e9 = ema(closes[-22:-1], 9)
    e21 = ema(closes[-22:-1], 21)
    e9_now = ema(closes[-21:], 9)
    e21_now = ema(closes[-21:], 21)
    if e9 <= e21 and e9_now > e21_now:
        return "long"   # bullish cross
    if e9 >= e21 and e9_now < e21_now:
        return "short"  # bearish cross
    return None


async def _persist_close(trader, result, session_id, strategy_id, user_id, underlying):
    """Write the closed options trade to the trades table with mode='options_paper'."""
    try:
        async with async_session_factory() as db:
            t = Trade(
                strategy_id=strategy_id,
                user_id=user_id,
                session_id=session_id,
                mode="options_paper",
                status="closed",
                instrument=f"{underlying} {result.contract.option_type.upper()} {result.contract.strike} {result.contract.expiration}",
                direction=result.direction,
                contracts=result.contracts,
                entry_price=result.entry_premium,
                exit_price=result.exit_premium,
                stop_loss=result.stop_premium,
                take_profit=result.target_premium,
                entry_time=result.entry_time,
                exit_time=result.exit_time,
                pnl=result.pnl,
                commission=result.commission,
                net_pnl=result.net_pnl,
                exit_reason=result.exit_reason,
                notes={
                    "underlying": underlying,
                    "strike": result.contract.strike,
                    "option_type": result.contract.option_type,
                    "expiration": str(result.contract.expiration),
                    "estimated_iv": result.estimated_iv,
                    "entry_spot": result.entry_spot,
                    "exit_spot": result.exit_spot,
                },
            )
            db.add(t)
            await db.commit()
    except Exception as e:
        logger.error(f"[OptionsPaperRunner] persist failed: {e}")


async def _run(session_id: str, strategy_id: str, user_id: str, underlying: str, watchlist: list[str] | None = None):
    try:
        # Load the strategy config
        async with async_session_factory() as db:
            strat = (await db.execute(select(Strategy).where(Strategy.id == strategy_id))).scalar_one_or_none()
        if not strat:
            logger.error(f"[OptionsPaperRunner] strategy {strategy_id} not found"); return

        # Pull initial spot to build the chain
        spot = _fetch_spot(underlying)
        if not spot:
            logger.error(f"[OptionsPaperRunner] no spot for {underlying} — aborting"); return

        chain = _build_synthetic_chain(underlying, spot)
        trader = OptionsPaperTrader(
            underlying=underlying,
            chain=chain,
            starting_balance=10_000.0,
            risk_per_trade_pct=float(getattr(strat, "options_risk_per_trade_pct", 1.5) or 1.5),
            stop_loss_premium_pct=50.0,
            target_premium_pct=float((strat.risk_reward_ratio or 2.0) * 100),
            session_id=session_id, user_id=user_id, strategy_id=strategy_id,
        )
        trader.start()
        logger.info(f"[OptionsPaperRunner] session {session_id} started | {underlying} | spot=${spot:.2f}")
        logger.info(
            f"[options-paper-runner] session_id={session_id} strategy={strat.name} "
            f"underlying={underlying} watchlist={watchlist} — running"
        )

        # Strategy-level config knobs
        delta_band = (float(getattr(strat, "options_target_delta_min", 0.30) or 0.30),
                       float(getattr(strat, "options_target_delta_max", 0.50) or 0.50))
        dte_band = (int(getattr(strat, "options_min_dte", 30) or 30),
                     int(getattr(strat, "options_max_dte", 60) or 60))
        prefer_itm = bool(getattr(strat, "options_prefer_itm", False))
        spread_width = getattr(strat, "options_spread_width", None)

        closes_buffer: list[float] = []
        last_processed_minute = None
        completed_count = 0
        last_heartbeat = datetime.now(timezone.utc)

        while True:
            try:
                # Check that session is still active
                async with async_session_factory() as db:
                    sess = (await db.execute(select(TradeSession).where(TradeSession.id == session_id))).scalar_one_or_none()
                    if not sess or not sess.is_active:
                        logger.info(f"[OptionsPaperRunner] session {session_id} stopped"); break

                # Heartbeat — every ~60s emit visible proof the runner is alive
                now = datetime.now(timezone.utc)
                if (now - last_heartbeat).total_seconds() >= 60:
                    pos = "open" if trader._position else "flat"
                    logger.info(
                        f"[options-paper-runner] sid={session_id} alive — "
                        f"{completed_count} fills today · pos={pos} · underlying={underlying}"
                    )
                    last_heartbeat = now

                # Pull recent bars
                df = yf.Ticker(underlying).history(period="2d", interval="1m")
                if df is None or df.empty:
                    await asyncio.sleep(60); continue
                latest_ts = df.index[-1]
                latest_close = float(df.iloc[-1]["Close"])
                today = date.today()

                # Update buffer when a new minute prints
                if last_processed_minute != latest_ts:
                    closes_buffer = [float(x) for x in df["Close"].tail(50).tolist()]
                    last_processed_minute = latest_ts

                # If we hold a position — mark-to-market
                if trader._position:
                    closed = trader.mark_to_market(spot=latest_close, today=today)
                    if closed:
                        completed_count += 1
                        logger.info(f"[OptionsPaperRunner] closed: {closed.exit_reason} | PnL=${closed.net_pnl:.2f}")
                        await _persist_close(trader, closed, session_id, strategy_id, user_id, underlying)

                # If no position — check for a signal
                if not trader._position and not trader._kill_switch:
                    side = _ema_signal(closes_buffer)
                    if side:
                        opened = trader.on_signal(
                            side=side, spot=latest_close, today=today,
                            delta_band=delta_band, dte_band=dte_band,
                            prefer_itm=prefer_itm,
                            spread_width=int(spread_width) if spread_width else None,
                            default_iv=0.30,
                        )
                        if opened:
                            logger.info(f"[OptionsPaperRunner] opened: {side} {opened.contract.option_type} "
                                         f"{opened.contract.strike} exp {opened.contract.expiration} @ ${opened.entry_premium:.2f}")

                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[OptionsPaperRunner] loop err: {e}")
                await asyncio.sleep(60)
    except Exception as e:
        logger.exception(f"[options-paper-runner] session={session_id} CRASHED: {e}")
    finally:
        _active.pop(session_id, None)
