#!/usr/bin/env python3
"""Replay a strategy over historical bars: NEW dedicated setup vs OLD generic
engine, bar-by-bar, as a before/after diff.

Build step 3 (proposal SS3.8) deliverable. For a given strategy name +
instrument + date range it:
  * loads the strategy's real config from the ``strategies`` table (session
    filters, RR, timeframes) - falling back to the seed defaults if the row is
    absent - so the replay uses the SAME knobs production would;
  * pulls bars per timeframe from the local ``candle_cache`` via
    ``fetch_from_cache`` (the existing data path; ETF-proxy rows are auto-scaled
    to the futures price level), and
  * walks the primary timeframe forward one bar at a time. At each step it
    assembles the multi-timeframe window the engine sees and evaluates BOTH:
      - NEW: the dedicated ``ICTSetup`` from the registry (``get_setup``), and
      - OLD: the generic ``ICTStrategy`` model (registry dispatch forced off),
    printing every bar where each FIRED (entry / stop / target / reason) and a
    final count so you can see whether the dedicated setup is more selective.

Usage (inside the backend container, against the worktree mounted at /opt/wt):
    python3 scripts/replay_strategy.py --strategy "FVG Inversion Tap" \
        --instrument ES --sessions 3
    python3 scripts/replay_strategy.py --strategy "FVG Inversion Tap" \
        --instrument ES --start 2026-06-05 --end 2026-06-10

This script is READ-ONLY: it fetches bars and evaluates in-memory. It places no
orders, writes no rows, sends no emails.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
from loguru import logger

from app.engines.data_feeds.local_cache import fetch_from_cache
from app.engines.strategy_engine.base_strategy import StrategyConfig, TradeSignal
from app.engines.backtest_engine import ict_strategy as ics
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.ict.context import ICTContext
from app.engines.ict import registry as reg


# --------------------------------------------------------------------------
# Config: prefer the real DB row, fall back to the seed defaults.
# --------------------------------------------------------------------------
async def _load_config(name: str, instrument: str) -> StrategyConfig:
    """Build a StrategyConfig for ``name`` from the strategies table; if absent,
    fall back to the seed defaults (so the replay still runs off-DB)."""
    try:
        from app.database import async_session_factory
        from sqlalchemy import text
        async with async_session_factory() as db:
            row = (await db.execute(
                text("SELECT name, primary_timeframe, execution_timeframe, "
                     "higher_timeframes, risk_reward_ratio, stop_loss_type, "
                     "stop_loss_ticks, max_contracts, session_filters, "
                     "fvg_min_size_ticks, max_trades_per_day, rule_tree "
                     "FROM strategies WHERE name = :n LIMIT 1"),
                {"n": name},
            )).fetchone()
    except Exception as exc:
        logger.warning(f"[replay] DB config lookup failed ({exc!r}); using seed defaults")
        row = None

    if row is not None:
        m = row._mapping
        cfg = StrategyConfig(
            name=m["name"], instruments=[instrument],
            primary_timeframe=m["primary_timeframe"] or "15m",
            execution_timeframe=m["execution_timeframe"] or "1m",
            higher_timeframes=list(m["higher_timeframes"] or []),
            risk_reward_ratio=float(m["risk_reward_ratio"] or 2.0),
            stop_loss_type=m["stop_loss_type"] or "structure",
            stop_loss_ticks=m["stop_loss_ticks"],
            max_contracts=int(m["max_contracts"] or 1),
            session_filters=list(m["session_filters"] or []),
            fvg_min_size_ticks=int(m["fvg_min_size_ticks"] or 4),
            max_trades_per_day=m["max_trades_per_day"],
        )
        cfg.rule_tree = dict(m["rule_tree"] or {})
        logger.info(f"[replay] loaded config from DB for {name!r}")
        return cfg

    # Seed fallback for the strategies this script is expected to target.
    _SEED = {
        "FVG Inversion Tap": dict(
            primary_timeframe="15m", execution_timeframe="1m",
            higher_timeframes=["1H", "4H"], risk_reward_ratio=3.0,
            session_filters=["NY_AM", "LONDON"], fvg_min_size_ticks=4,
        ),
    }
    s = _SEED.get(name, dict(
        primary_timeframe="15m", execution_timeframe="1m",
        higher_timeframes=["1H"], risk_reward_ratio=2.0,
        session_filters=[], fvg_min_size_ticks=4,
    ))
    cfg = StrategyConfig(name=name, instruments=[instrument], max_contracts=3, **s)
    cfg.rule_tree = {}
    logger.info(f"[replay] using SEED defaults for {name!r}")
    return cfg


# --------------------------------------------------------------------------
# Bar loading.
# --------------------------------------------------------------------------
async def _load_bars(instrument: str, tfs: list[str],
                     start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for tf in tfs:
        df = await fetch_from_cache(instrument, start, end, tf)
        if df is not None and not df.empty:
            out[tf] = df
    return out


# --------------------------------------------------------------------------
# OLD generic engine: force the registry dispatch OFF so on_bar runs the
# pre-port generic model (its body falls through to generic when get_setup
# returns None). We monkeypatch the symbol ict_strategy imported.
# --------------------------------------------------------------------------
@contextmanager
def _generic_dispatch_disabled():
    orig = ics.get_setup
    ics.get_setup = lambda *a, **k: None  # always fall back to generic
    try:
        yield
    finally:
        ics.get_setup = orig


def _fmt(sig: Optional[TradeSignal]) -> str:
    if sig is None:
        return "-"
    return (f"{sig.signal.value.upper()} entry={sig.entry_price:.2f} "
            f"stop={sig.stop_loss:.2f} tgt={sig.take_profit:.2f}")


def _reason(sig: Optional[TradeSignal]) -> str:
    if sig is None:
        return ""
    md = sig.metadata or {}
    if md.get("setup") == "fvg_inversion_tap":
        return (f"ifvg_inversion sweep={md.get('sweep_level')} "
                f"fvg={md.get('fvg_type')}->{md.get('bias')}")
    return (f"bias={md.get('bias')} fvg={md.get('fvg_type')} "
            f"inversion={md.get('inversion')} sweep={md.get('sweep_level')}")


# --------------------------------------------------------------------------
# Replay.
# --------------------------------------------------------------------------
async def replay(name: str, instrument: str, start: datetime, end: datetime,
                 warmup: int = 60) -> None:
    cfg = await _load_config(name, instrument)
    all_tfs = list(dict.fromkeys(
        [cfg.primary_timeframe, cfg.execution_timeframe, *cfg.higher_timeframes]
    ))
    print(f"\n=== REPLAY {name!r} on {instrument} "
          f"{start.date()}..{end.date()} ===")
    print(f"config: primary={cfg.primary_timeframe} exec={cfg.execution_timeframe} "
          f"htf={cfg.higher_timeframes} rr={cfg.risk_reward_ratio} "
          f"sessions={cfg.session_filters} rule_tree={getattr(cfg, 'rule_tree', {})}")

    bars = await _load_bars(instrument, all_tfs, start, end)
    if cfg.primary_timeframe not in bars or cfg.execution_timeframe not in bars:
        print(f"!! insufficient cached bars (have {list(bars.keys())}); "
              f"need {cfg.primary_timeframe}+{cfg.execution_timeframe}. Aborting.")
        return
    for tf, df in bars.items():
        print(f"  loaded {tf}: {len(df)} bars {df.index[0]} .. {df.index[-1]}")

    exec_tf = cfg.execution_timeframe
    exec_df = bars[exec_tf]
    exec_index = exec_df.index

    # Dedicated setup instance (shared across bars so its per-day cap behaves
    # like a live session). None if the name is not ported.
    dedicated = reg.get_setup(cfg.name, getattr(cfg, "rule_tree", {}) or {})
    if dedicated is None:
        print(f"  NOTE: {name!r} is NOT ported (get_setup=None) -> NEW column "
              f"will mirror the generic fallback.")

    # Generic engine instance (registry dispatch forced off in the loop).
    generic = ICTStrategy(cfg, instrument=instrument)

    old_fires: list[tuple] = []
    new_fires: list[tuple] = []

    # Walk the EXECUTION timeframe forward (that's where the inversion fires).
    # At each step, assemble the window of every TF up to the current exec ts.
    n = len(exec_index)
    for i in range(warmup, n):
        ts = exec_index[i]
        window: dict[str, pd.DataFrame] = {}
        for tf, df in bars.items():
            sub = df[df.index <= ts]
            if len(sub):
                window[tf] = sub
        # primary must have enough bars for the generic model (needs >=15)
        if cfg.primary_timeframe not in window or len(window[cfg.primary_timeframe]) < 15:
            continue

        # --- NEW: dedicated setup (fresh ICTContext from the window) ---
        new_sig = None
        if dedicated is not None:
            try:
                new_sig = dedicated.evaluate(ICTContext.from_bars(window, instrument, cfg))
            except Exception as exc:
                logger.warning(f"[replay] dedicated evaluate raised @ {ts}: {exc!r}")

        # --- OLD: generic engine (dispatch disabled) ---
        old_sig = None
        try:
            with _generic_dispatch_disabled():
                old_sig = generic.on_bar(window)
        except Exception as exc:
            logger.warning(f"[replay] generic on_bar raised @ {ts}: {exc!r}")

        if old_sig is not None:
            old_fires.append((ts, old_sig))
        if new_sig is not None:
            new_fires.append((ts, new_sig))

        if old_sig is not None or new_sig is not None:
            print(f"\n[{ts}]")
            print(f"   OLD generic : {_fmt(old_sig)}  {_reason(old_sig)}")
            print(f"   NEW setup   : {_fmt(new_sig)}  {_reason(new_sig)}")

    print("\n=== SUMMARY (before/after) ===")
    print(f"  OLD generic-engine signals : {len(old_fires)}")
    print(f"  NEW dedicated-setup signals: {len(new_fires)}")
    if len(new_fires) < len(old_fires):
        print(f"  -> dedicated setup is MORE selective "
              f"({len(old_fires) - len(new_fires)} fewer fires).")
    elif len(new_fires) == len(old_fires):
        print("  -> same number of fires.")
    else:
        print(f"  -> dedicated setup fired MORE ({len(new_fires) - len(old_fires)} more).")


def _sessions_to_range(sessions: int) -> tuple[datetime, datetime]:
    """Approximate 'last N sessions' as the last N*~1.6 calendar days back from
    now (covers weekends/holidays loosely; we then clip to available bars)."""
    end = datetime.now(timezone.utc)
    # pull a generous window; the cache + session filter clip it to real bars.
    start = end - timedelta(days=max(3, int(sessions * 2) + 2))
    return start, end


def main() -> None:
    ap = argparse.ArgumentParser(description="Replay strategy: new setup vs old generic engine.")
    ap.add_argument("--strategy", required=True, help="strategy name (e.g. 'FVG Inversion Tap')")
    ap.add_argument("--instrument", default="ES")
    ap.add_argument("--sessions", type=int, default=None, help="last N sessions (approx)")
    ap.add_argument("--start", default=None, help="YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="YYYY-MM-DD")
    ap.add_argument("--warmup", type=int, default=60, help="exec bars to skip before evaluating")
    args = ap.parse_args()

    if args.start and args.end:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) + timedelta(days=1)
    else:
        start, end = _sessions_to_range(args.sessions or 3)

    asyncio.run(replay(args.strategy, args.instrument, start, end, warmup=args.warmup))


if __name__ == "__main__":
    main()
