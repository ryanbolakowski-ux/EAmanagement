"""
Strategy V2 harness — V1 vs V2 execution-assumption comparison (v2-redesign).

Runs every strategy twice through the REAL backtest engine (same import
pattern as app/api/routes/backtests.py::_run_backtest_task):

  * V1 leg — the engine's V1 execution assumptions (slippage_ticks=1,
    matching the BacktestConfig default the V1 audit ran on).
  * V2 leg — the harsher V2 assumptions under evaluation for the redesign
    (slippage_ticks=2 by default; override with --slippage-ticks).

Both legs see identical bars and identical strategy logic, so every delta in
the output table is attributable purely to the execution assumptions.

The window is also split into in-sample / out-of-sample segments
(--oos-fraction, default 0.3 = last 30% of the window is OOS) so we can see
whether the V1-vs-V2 spread is stable out of sample. The engine runs ONCE per
leg over the full window; segments are sliced from the completed trade list
(same trade set, no re-run drift).

Output: docs/v2/strategy-v2-comparison.{csv,md} with WR, PF, DD%, net $,
avg R, trade count per leg plus the V2-V1 delta for each.

Run (real data — needs the DB + candle cache, i.e. inside the backend
container; NEVER point this at prod from a worktree):
    python -m scripts.strategy_v2_harness --all --start 2026-04-01 --end 2026-06-13
    python -m scripts.strategy_v2_harness --strategy-name "AMD Strategy" --start 2026-04-01 --end 2026-06-13

Run (synthetic bars, deterministic seed — NO DB required; this is the CI /
smoke path):
    python -m scripts.strategy_v2_harness --all --synthetic --start 2026-04-01 --end 2026-05-01
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger

# Engine imports — deliberately only the DB-free modules at top level so
# --synthetic runs need neither DATABASE_URL nor any model import. The DB
# path imports its dependencies inside run_from_db() (route pattern).
from app.engines.backtest_engine.backtest_runner import (
    BacktestConfig, BacktestRunner, TICK_SIZES,
)
from app.engines.backtest_engine.data_handler import DataHandler
from app.engines.backtest_engine.metrics import calculate_metrics
from app.engines.strategy_engine.base_strategy import (
    BaseStrategy, SignalType, StrategyConfig, TradeSignal,
)


# scripts/ -> backend/ -> repo root. In the deployed container /app IS the
# backend, so this resolves to /docs/v2 — mount or override with --output-dir.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "v2"

CSV_NAME = "strategy-v2-comparison.csv"
MD_NAME = "strategy-v2-comparison.md"

# Default synthetic book for `--synthetic --all`. Cadence / stop / RR vary so
# the comparison table exercises different trade counts and R profiles.
SYNTHETIC_BOOK: list[tuple[str, str, dict]] = [
    ("Synthetic Pulse Fast", "ES", dict(signal_every=23, stop_ticks=8, rr=2.0)),
    ("Synthetic Pulse Mid",  "ES", dict(signal_every=41, stop_ticks=12, rr=2.5)),
    ("Synthetic Pulse Slow", "NQ", dict(signal_every=67, stop_ticks=16, rr=3.0)),
]

FIELDNAMES = [
    "strategy", "instrument", "segment",
    "v1_trades", "v1_wr_pct", "v1_pf", "v1_dd_pct", "v1_net_usd", "v1_avg_r",
    "v2_trades", "v2_wr_pct", "v2_pf", "v2_dd_pct", "v2_net_usd", "v2_avg_r",
    "delta_trades", "delta_wr_pct", "delta_pf", "delta_dd_pct", "delta_net_usd", "delta_avg_r",
]


# ─────────────────────────────────────────────────────────────────────────────
# In-sample / out-of-sample split
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OOSSplit:
    """Time boundaries of the IS/OOS split. Invariant: is_end == oos_start
    (a trade belongs to OOS iff entry_time >= oos_start)."""
    is_start: datetime
    is_end: datetime
    oos_start: datetime
    oos_end: datetime


def compute_oos_split(start: datetime, end: datetime, oos_fraction: float) -> OOSSplit:
    """Split [start, end] so the LAST `oos_fraction` of the window is
    out-of-sample. 0.0 = everything in-sample (OOS empty). The boundary is
    time-based, not trade-count-based, so both legs share the same cut."""
    if end <= start:
        raise ValueError(f"end ({end}) must be after start ({start})")
    if not (0.0 <= oos_fraction < 1.0):
        raise ValueError(f"oos_fraction must be in [0, 1), got {oos_fraction}")
    cut = end - (end - start) * float(oos_fraction)
    return OOSSplit(is_start=start, is_end=cut, oos_start=cut, oos_end=end)


def _naive_utc(dt: datetime) -> datetime:
    """Trade entry times come back tz-aware (bars are UTC-localized); the CLI
    window is naive. Normalize to naive-UTC so segment comparisons work."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def segment_trades(trades: list[dict], segment: str, split: OOSSplit) -> list[dict]:
    if segment == "full":
        return list(trades)
    if segment == "in_sample":
        return [t for t in trades if _naive_utc(t["entry_time"]) < split.oos_start]
    if segment == "out_of_sample":
        return [t for t in trades if _naive_utc(t["entry_time"]) >= split.oos_start]
    raise ValueError(f"unknown segment {segment!r}")


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic data + synthetic strategy (the no-DB test path)
# ─────────────────────────────────────────────────────────────────────────────

def generate_synthetic_bars(start: datetime, end: datetime, seed: int = 42,
                            start_price: float = 5000.0, tick: float = 0.25) -> pd.DataFrame:
    """Deterministic 1m OHLCV random walk snapped to the tick grid. Same seed
    -> byte-identical bars, so both legs (and CI reruns) see the same market."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start=start, end=end, freq="1min", inclusive="left")
    n = len(idx)
    if n < 100:
        raise ValueError(f"synthetic window too short ({n} bars) — widen --start/--end")
    # Mild regime wave on top of the walk so both directions get traversed.
    steps = rng.normal(0.0, 1.6, n) + 0.35 * np.sin(np.arange(n) / 240.0)
    closes = start_price + np.cumsum(steps)
    opens = np.empty(n)
    opens[0] = start_price
    opens[1:] = closes[:-1]
    wick = np.abs(rng.normal(0.0, 1.2, n)) + 0.5
    highs = np.maximum(opens, closes) + wick
    lows = np.minimum(opens, closes) - wick

    def snap(a: np.ndarray) -> np.ndarray:
        return np.round(a / tick) * tick

    return pd.DataFrame({
        "timestamp": idx,
        "open": snap(opens), "high": snap(highs),
        "low": snap(lows), "close": snap(closes),
        "volume": rng.integers(50, 500, n).astype(float),
    })


class SyntheticPulseStrategy(BaseStrategy):
    """Deterministic pulse strategy for --synthetic runs. Emits an alternating
    long/short signal every `signal_every` flat bars with a fixed tick stop and
    the config's R:R target. It exercises the REAL BacktestRunner fill /
    slippage / commission / sizing path with zero market-structure dependency,
    so every V1-vs-V2 delta is attributable purely to the execution
    assumptions under test (this is a harness fixture, not a tradable edge)."""

    def __init__(self, config: StrategyConfig, instrument: str = "ES",
                 signal_every: int = 29, stop_ticks: int = 8):
        super().__init__(config)
        self.instrument = instrument
        self.tick_size = TICK_SIZES.get(instrument, 0.25)
        self.signal_every = max(2, int(signal_every))
        self.stop_ticks = max(2, int(stop_ticks))
        self._flat_bar_count = 0
        self._signal_count = 0

    def on_bar(self, bars: dict[str, pd.DataFrame]) -> Optional[TradeSignal]:
        ptf = self.config.primary_timeframe
        df = bars.get(ptf)
        if df is None or len(df) < 2:
            return None
        self._flat_bar_count += 1
        if self._flat_bar_count % self.signal_every:
            return None
        if not self.check_risk_controls():
            return None
        self._signal_count += 1
        direction = SignalType.LONG if self._signal_count % 2 else SignalType.SHORT
        close = float(df.iloc[-1]["close"])
        risk = self.stop_ticks * self.tick_size
        if direction == SignalType.LONG:
            sl = close - risk
            tp = self.compute_take_profit(close, sl, "long")
        else:
            sl = close + risk
            tp = self.compute_take_profit(close, sl, "short")
        return TradeSignal(
            signal=direction, instrument=self.instrument,
            entry_price=close, stop_loss=sl, take_profit=tp,
            contracts=self.config.max_contracts,
            metadata={"synthetic": True, "pulse": self._signal_count},
        )

    def on_tick(self, tick: dict) -> Optional[TradeSignal]:
        return None


def _spec_for_name(name: str) -> tuple[str, str, dict]:
    """Resolve a synthetic spec for a strategy name. Book names get their
    curated spec; unknown names get a crc32-derived spec (stable across runs —
    unlike hash(), crc32 ignores PYTHONHASHSEED)."""
    for book_name, inst, spec in SYNTHETIC_BOOK:
        if book_name.lower() == name.lower():
            return book_name, inst, dict(spec)
    h = zlib.crc32(name.encode("utf-8"))
    return name, "ES", dict(
        signal_every=23 + (h % 5) * 11,
        stop_ticks=8 + ((h >> 3) % 3) * 4,
        rr=2.0 + ((h >> 5) % 3) * 0.5,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Leg execution + comparison rows
# ─────────────────────────────────────────────────────────────────────────────

def trade_dicts(trades) -> list[dict]:
    """SimulatedTrade -> the dict shape calculate_metrics consumes."""
    return [
        {
            "entry_time": t.entry_time,
            "exit_time": t.exit_time,
            "net_pnl": t.net_pnl,
            "is_winner": t.is_winner,
            "exit_reason": t.exit_reason,
        }
        for t in trades
    ]


def run_leg(strategy: BaseStrategy, bars_df: pd.DataFrame, instrument: str,
            start: datetime, end: datetime, slippage_ticks: int, *,
            base_timeframe: str, primary_timeframe: str, all_timeframes: list[str],
            initial_capital: float, risk_per_trade_pct: float,
            commission_per_side: float = 2.25,
            breakeven_at_r: float = 0.0, breakeven_mode: str = "off") -> list[dict]:
    """One engine pass with the given slippage assumption. Fresh DataHandler
    per leg — filter_date_range mutates the handler in place, and strategy
    state must never bleed between legs."""
    dh = DataHandler(instrument=instrument, base_timeframe=base_timeframe)
    dh.load_from_dataframe(bars_df)
    cfg = BacktestConfig(
        instrument=instrument,
        start_date=start,
        end_date=end,
        primary_timeframe=primary_timeframe,
        all_timeframes=all_timeframes,
        initial_capital=initial_capital,
        commission_per_side=commission_per_side,
        slippage_ticks=slippage_ticks,
        risk_per_trade_pct=risk_per_trade_pct,
        breakeven_at_r=breakeven_at_r,
        breakeven_mode=breakeven_mode,
    )
    runner = BacktestRunner(strategy, dh, cfg)
    runner.run()
    return trade_dicts(runner.completed_trades)


def _leg_cols(prefix: str, m) -> dict:
    return {
        f"{prefix}_trades": m.total_trades,
        f"{prefix}_wr_pct": m.win_rate * 100.0,
        f"{prefix}_pf": m.profit_factor,
        f"{prefix}_dd_pct": m.max_drawdown_pct,
        f"{prefix}_net_usd": m.net_profit,
        f"{prefix}_avg_r": m.avg_rr,
    }


def _delta(v1, v2):
    """V2 - V1, or None when either side is non-finite (e.g. PF=inf on a
    no-loss segment) — an inf delta is noise, not signal."""
    try:
        if any(isinstance(v, float) and (math.isinf(v) or math.isnan(v)) for v in (v1, v2)):
            return None
        return v2 - v1
    except TypeError:
        return None


def build_comparison_rows(name: str, instrument: str, v1_trades: list[dict],
                          v2_trades: list[dict], split: OOSSplit,
                          initial_capital: float) -> list[dict]:
    """Three rows per strategy: full / in_sample / out_of_sample, each with
    both legs' metrics plus the V2-V1 delta per metric."""
    rows = []
    for segment in ("full", "in_sample", "out_of_sample"):
        m1 = calculate_metrics(segment_trades(v1_trades, segment, split), initial_capital)
        m2 = calculate_metrics(segment_trades(v2_trades, segment, split), initial_capital)
        c1, c2 = _leg_cols("v1", m1), _leg_cols("v2", m2)
        row = {"strategy": name, "instrument": instrument, "segment": segment, **c1, **c2}
        for metric in ("trades", "wr_pct", "pf", "dd_pct", "net_usd", "avg_r"):
            row[f"delta_{metric}"] = _delta(c1[f"v1_{metric}"], c2[f"v2_{metric}"])
        rows.append(row)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Output writers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_cell(key: str, v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v  # label columns (strategy / instrument / segment)
    if key.endswith("_trades") or key == "delta_trades":
        return str(int(v))
    if isinstance(v, float) and math.isnan(v):
        return ""
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    if key.endswith("_wr_pct") or key.endswith("_dd_pct"):
        return f"{v:.1f}"
    if key.endswith("_net_usd"):
        return f"{v:.2f}"
    return f"{v:.2f}"


def write_outputs(rows: list[dict], out_dir: Path, meta: dict) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / CSV_NAME
    md_path = out_dir / MD_NAME

    with open(csv_path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _fmt_cell(k, row.get(k)) for k in FIELDNAMES})

    lines = [
        "# Strategy V2 — execution-assumption comparison (V1 vs V2)",
        "",
        (f"_Generated {meta['generated_at']} · window {meta['start']} → {meta['end']} · "
         f"V1 slippage {meta['v1_slippage_ticks']} tick(s)/side vs V2 slippage "
         f"{meta['v2_slippage_ticks']} tick(s)/side · OOS fraction {meta['oos_fraction']} "
         f"(OOS starts {meta['oos_start']}) · data: {meta['data_source']}._"),
        "",
        ("Both legs run identical strategy logic on identical bars — the delta "
         "columns isolate the cost of the V2 execution assumptions. Deltas are "
         "V2 − V1; blank delta = one side non-finite (e.g. PF=inf on a "
         "no-loss segment)."),
        "",
        "| Strategy | Inst | Segment | Trades V1 | Trades V2 | ΔTr | WR% V1 | WR% V2 | ΔWR | PF V1 | PF V2 | ΔPF | DD% V1 | DD% V2 | ΔDD | Net$ V1 | Net$ V2 | ΔNet$ | avgR V1 | avgR V2 | ΔavgR |",
        "|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    md_order = [
        "v1_trades", "v2_trades", "delta_trades",
        "v1_wr_pct", "v2_wr_pct", "delta_wr_pct",
        "v1_pf", "v2_pf", "delta_pf",
        "v1_dd_pct", "v2_dd_pct", "delta_dd_pct",
        "v1_net_usd", "v2_net_usd", "delta_net_usd",
        "v1_avg_r", "v2_avg_r", "delta_avg_r",
    ]
    for row in rows:
        cells = [row["strategy"], row["instrument"], row["segment"]]
        cells += [_fmt_cell(k, row.get(k)) for k in md_order]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    md_path.write_text("\n".join(lines))
    return csv_path, md_path


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic mode (no DB)
# ─────────────────────────────────────────────────────────────────────────────

def run_synthetic(args: argparse.Namespace) -> list[dict]:
    names = args.strategy_names or [n for n, _, _ in SYNTHETIC_BOOK]
    split = compute_oos_split(args.start_dt, args.end_dt, args.oos_fraction)
    rows: list[dict] = []
    for name in names:
        disp_name, inst, spec = _spec_for_name(name)
        # Seed offset per instrument so ES and NQ don't trade the same tape;
        # everything stays deterministic for a given --seed.
        bars = generate_synthetic_bars(
            args.start_dt, args.end_dt,
            seed=args.seed + (zlib.crc32(inst.encode("utf-8")) % 997),
        )
        legs: dict[str, list[dict]] = {}
        for label, slip in (("v1", args.v1_slippage_ticks), ("v2", args.slippage_ticks)):
            cfg = StrategyConfig(
                name=f"{disp_name} [{label}]",
                instruments=[inst],
                primary_timeframe="1m",
                execution_timeframe="1m",
                higher_timeframes=["15m"],
                risk_reward_ratio=float(spec["rr"]),
                max_contracts=10,
            )
            strategy = SyntheticPulseStrategy(
                cfg, instrument=inst,
                signal_every=spec["signal_every"], stop_ticks=spec["stop_ticks"],
            )
            legs[label] = run_leg(
                strategy, bars, inst, args.start_dt, args.end_dt, slip,
                base_timeframe="1m", primary_timeframe="1m",
                all_timeframes=["1m", "15m"],
                initial_capital=args.initial_capital,
                risk_per_trade_pct=args.risk_per_trade_pct,
            )
        logger.info(f"[harness] {disp_name}: v1={len(legs['v1'])} trades, v2={len(legs['v2'])} trades")
        rows += build_comparison_rows(disp_name, inst, legs["v1"], legs["v2"], split, args.initial_capital)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Real-data mode (DB + candle cache — same pattern as the backtests route)
# ─────────────────────────────────────────────────────────────────────────────

async def run_from_db(args: argparse.Namespace) -> list[dict]:
    # Route-pattern imports, kept inside the function so --synthetic never
    # touches the DB modules (see _run_backtest_task in routes/backtests.py).
    from sqlalchemy import select
    import app.models  # noqa: F401 — full mapper registry so relationships resolve
    from app.database import async_session_factory
    from app.models.strategy import Strategy, StrategyStatus
    from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
    from app.engines.backtest_engine.ict_strategy import ICTStrategy

    split = compute_oos_split(args.start_dt, args.end_dt, args.oos_fraction)

    logger.info("[harness] connecting to db …")
    async with async_session_factory() as session:
        # status column is VARCHAR in prod while the model declares a PG enum —
        # the ORM comparison casts to ::strategystatus and fails (asyncpg f405).
        # Match the codebase convention: compare as text (see routes/*.py).
        from sqlalchemy import cast, String
        # DB stores UPPERCASE (raw-SQL convention across the codebase: status = ACTIVE)
        # while StrategyStatus.ACTIVE.value is lowercase active — compare the literal.
        result = await session.execute(
            select(Strategy).where(cast(Strategy.status, String) == "ACTIVE")
        )
        strategies = result.scalars().all()
        logger.info(f"[harness] loaded {len(strategies)} ACTIVE strategies from db")

    # One job per NAME (the book has per-user duplicates of the same
    # definition — the V1 audit reported one row per name too).
    jobs: list[tuple] = []
    seen: set[str] = set()
    for s in strategies:
        if args.strategy_names and s.name not in args.strategy_names:
            continue
        if s.name in seen:
            continue
        inst = next((str(i).upper() for i in (s.instruments or [])
                     if str(i).upper() in TICK_SIZES), None)
        if inst is None:
            continue  # options / stock strategies — not this harness's lane
        seen.add(s.name)
        jobs.append((s, inst))

    if not jobs:
        logger.warning("[harness] no matching active futures strategies")
        return []

    rows: list[dict] = []
    data_cache: dict[tuple, pd.DataFrame] = {}
    for s, inst in jobs:
        exec_tf = s.execution_timeframe or "1m"
        key = (inst, exec_tf)
        try:
            if key not in data_cache:
                data_cache[key] = await fetch_futures_data(
                    instrument=inst,
                    start_date=args.start_dt,
                    end_date=args.end_dt,
                    interval=exec_tf,
                    use_polygon=True,
                )
            df = data_cache[key]
            if df is None or df.empty:
                logger.warning(f"[harness] no data for {inst} {exec_tf} — skipping {s.name!r}")
                continue

            legs: dict[str, list[dict]] = {}
            for label, slip in (("v1", args.v1_slippage_ticks), ("v2", args.slippage_ticks)):
                # Same StrategyConfig build as _run_backtest_task — the legs
                # must run the exact engine the product runs.
                cfg = StrategyConfig(
                    name=s.name,
                    instruments=s.instruments or [inst],
                    primary_timeframe=s.primary_timeframe or "15m",
                    execution_timeframe=exec_tf,
                    higher_timeframes=s.higher_timeframes or [],
                    risk_reward_ratio=s.risk_reward_ratio or 2.0,
                    stop_loss_type=s.stop_loss_type or "structure",
                    stop_loss_ticks=s.stop_loss_ticks,
                    max_contracts=s.max_contracts or 1,
                    session_filters=s.session_filters or [],
                    fvg_min_size_ticks=s.fvg_min_size_ticks or 4,
                    fvg_max_size_ticks=s.fvg_max_size_ticks,
                    max_daily_loss=s.max_daily_loss,
                    max_trades_per_day=s.max_trades_per_day,
                    use_rsi_filter=bool((s.rule_tree or {}).get("use_rsi_filter", False)),
                    use_vwap_filter=bool((s.rule_tree or {}).get("use_vwap_filter", False)),
                )
                cfg.rule_tree = s.rule_tree or {}
                cfg.take_profit_mode = (s.rule_tree or {}).get("take_profit_mode", "auto")
                strategy = ICTStrategy(cfg, instrument=inst)

                all_tfs = list(set([cfg.primary_timeframe, cfg.execution_timeframe] + cfg.higher_timeframes))
                legs[label] = run_leg(
                    strategy, df.reset_index(), inst, args.start_dt, args.end_dt, slip,
                    base_timeframe=exec_tf,
                    primary_timeframe=cfg.primary_timeframe,
                    all_timeframes=all_tfs,
                    initial_capital=args.initial_capital,
                    risk_per_trade_pct=args.risk_per_trade_pct,
                    breakeven_at_r=float(getattr(s, "breakeven_at_r", None) or 0.0),
                    breakeven_mode=str(getattr(s, "breakeven_mode", None) or "off"),
                )
            logger.info(f"[harness] {s.name}: v1={len(legs['v1'])} trades, v2={len(legs['v2'])} trades")
            rows += build_comparison_rows(s.name, inst, legs["v1"], legs["v2"], split, args.initial_capital)
        except Exception as exc:
            # One broken strategy must not sink the whole comparison run.
            logger.error(f"[harness] {s.name!r} failed: {type(exc).__name__}: {exc}")
            continue
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Compare V1 vs V2 execution assumptions per strategy "
                    "(see module docstring).")
    ap.add_argument("--strategy-name", action="append", dest="strategy_names",
                    metavar="NAME", help="strategy name to run (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="run every active futures strategy (or the whole synthetic book)")
    ap.add_argument("--start", default="2026-04-01", help="window start YYYY-MM-DD")
    ap.add_argument("--end", default="2026-06-13", help="window end YYYY-MM-DD")
    ap.add_argument("--slippage-ticks", type=int, default=2,
                    help="V2 leg slippage in ticks/side (default 2)")
    ap.add_argument("--v1-slippage-ticks", type=int, default=1,
                    help="V1 leg slippage — the engine's V1 config default (1)")
    ap.add_argument("--oos-fraction", type=float, default=0.3,
                    help="fraction of the window held out-of-sample at the END (default 0.3)")
    ap.add_argument("--synthetic", action="store_true",
                    help="run on deterministic generated bars — no DB required")
    ap.add_argument("--seed", type=int, default=42, help="synthetic bar seed (default 42)")
    ap.add_argument("--initial-capital", type=float, default=100_000.0)
    ap.add_argument("--risk-per-trade-pct", type=float, default=1.0)
    ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                    help=f"where to write {CSV_NAME} / {MD_NAME}")
    ap.add_argument("--verbose", action="store_true", help="keep engine DEBUG logging")
    args = ap.parse_args(argv)

    if not args.all and not args.strategy_names:
        ap.error("provide --strategy-name or --all")
    try:
        args.start_dt = datetime.strptime(args.start, "%Y-%m-%d")
        args.end_dt = datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError as exc:
        ap.error(f"bad date: {exc}")
    if args.end_dt <= args.start_dt:
        ap.error("--end must be after --start")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if not args.verbose:
        # The engine logs every entry/exit at DEBUG — mute for a readable table.
        logger.remove()
        logger.add(sys.stderr, level="INFO")

    if args.synthetic:
        rows = run_synthetic(args)
        data_source = f"synthetic deterministic bars (seed {args.seed})"
    else:
        import asyncio
        rows = asyncio.run(run_from_db(args))
        data_source = "live candle cache / Polygon futures"

    split = compute_oos_split(args.start_dt, args.end_dt, args.oos_fraction)
    meta = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "start": args.start, "end": args.end,
        "v1_slippage_ticks": args.v1_slippage_ticks,
        "v2_slippage_ticks": args.slippage_ticks,
        "oos_fraction": args.oos_fraction,
        "oos_start": split.oos_start.strftime("%Y-%m-%d %H:%M"),
        "data_source": data_source,
    }
    csv_path, md_path = write_outputs(rows, Path(args.output_dir), meta)
    logger.info(f"[harness] wrote {csv_path} and {md_path} ({len(rows)} rows)")

    # Echo the table so a terminal run needs no file-hopping.
    print(md_path.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
