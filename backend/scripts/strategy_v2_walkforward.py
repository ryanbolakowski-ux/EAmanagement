"""
Strategy V2 walk-forward optimizer — Phase-B batch driver (v2-redesign).

For every active futures strategy (or the synthetic book with --synthetic):

  1. Build a modest parameter grid around the V2 tunables:
         risk_reward_ratio  ∈ {1.5, 2.0, 2.5, 3.0}
         breakeven_at_r     ∈ {off, 0.5R, 0.75R}    (breakeven_mode "r" when on)
         fvg_min_size_ticks ∈ {2, 4, 6}             (only when the strategy's
                                                      engine path consumes FVGs)
     The strategy's CURRENT (V1) params are ALWAYS included as one labeled
     combo so the comparison is apples-to-apples. The grid is pruned
     (deterministic, evenly spaced) to --max-combos.

  2. Walk-forward split: the LAST --oos-fraction of the window is held out
     (identical split_walkforward / TRAIN_END_EPSILON rule as the live
     optimizer, so no holdout bar leaks into training). Every combo is
     backtested on the TRAIN window ONLY via the REAL optimizer worker
     (opt_worker.run_combo, ProcessPool, same initializer) — no re-implemented
     scoring.

  3. The top 5 combos by TRAIN profit factor (min 20 train trades) PLUS the
     V1-params combo are then evaluated on the OOS window at 2-tick slippage.
     BOTH legs — V1 included — get the same execution realism; V1 does NOT
     keep the optimistic 1-tick treatment in the showdown. The best OOS
     profit factor with at least 8 OOS trades is the "V2 candidate".

  4. One row per strategy: V1-params OOS metrics vs V2-candidate OOS metrics,
     the winning param diff (e.g. "rr 2.5→2, BE 0.75R→0.5R") and a verdict
     (V2_BETTER / V1_HOLDS / INSUFFICIENT_TRADES). Rows are APPENDED to
     docs/v2/strategy-v2-optimized.{csv,md} after EACH strategy so partial
     results survive a kill (header is written first).

Run (real data — inside the backend container; NEVER point this at prod from
a worktree):
    python -m scripts.strategy_v2_walkforward --all --start 2026-04-01 --end 2026-06-13
    python -m scripts.strategy_v2_walkforward --strategy-name "AMD Strategy" \
        --start 2026-04-01 --end 2026-06-13 --workers 3

Run (synthetic deterministic bars — no DB required; the CI / smoke path):
    python -m scripts.strategy_v2_walkforward --synthetic --seed 42 --max-combos 12
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import math
import multiprocessing as mp
import sys
import zlib
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

# DB-free engine imports only at top level (the --synthetic path must run
# without DATABASE_URL). DB modules are imported inside _load_db_jobs().
from app.engines.backtest_engine.backtest_runner import (
    BacktestConfig, BacktestRunner, TICK_SIZES,
)
from app.engines.backtest_engine.data_handler import DataHandler
from app.engines.strategy_engine.base_strategy import StrategyConfig

# REUSE the live optimizer's worker: per-process DataHandler init, the combo
# runner (train phase), and the walk-forward split rule. Do not reimplement.
from app.engines.optimization_engine import opt_worker
from app.engines.optimization_engine.opt_worker import (
    TRAIN_END_EPSILON, split_walkforward,
)

# Synthetic fixtures come from the Phase-A harness so both scripts smoke-test
# against the exact same deterministic tape and pulse strategy.
from scripts.strategy_v2_harness import (
    SYNTHETIC_BOOK, SyntheticPulseStrategy, _spec_for_name,
    generate_synthetic_bars,
)


# scripts/ -> backend/ -> repo root; in the container /app IS the backend so
# this resolves to /docs/v2 — mount or override with --output-dir.
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "docs" / "v2"
CSV_NAME = "strategy-v2-optimized.csv"
MD_NAME = "strategy-v2-optimized.md"

# ── Grid axes (kept deliberately modest: 4×3×3 = 36 combos max, 12 without
#    the FVG axis — under the default --max-combos 48 before pruning) ────────
RR_AXIS = [1.5, 2.0, 2.5, 3.0]
BE_AXIS = [(0.0, "off"), (0.5, "r"), (0.75, "r")]   # (breakeven_at_r, mode)
FVG_AXIS = [2, 4, 6]

TOP_N_TRAIN = 5        # combos advanced from train ranking to the OOS showdown
MIN_TRAIN_TRADES = 20  # a combo must produce this many train trades to rank
MIN_OOS_TRADES = 8     # ... and this many OOS trades to become the candidate

FIELDNAMES = [
    "strategy", "instrument", "verdict",
    "v1_oos_trades", "v1_oos_wr_pct", "v1_oos_pf", "v1_oos_dd_pct", "v1_oos_net_usd",
    "v2_oos_trades", "v2_oos_wr_pct", "v2_oos_pf", "v2_oos_dd_pct", "v2_oos_net_usd",
    "v2_train_trades", "v2_train_pf", "param_diff", "v2_params_json",
]


# ─────────────────────────────────────────────────────────────────────────────
# Parameter grid
# ─────────────────────────────────────────────────────────────────────────────

def _combo_key(p: dict) -> tuple:
    """Canonical identity of a combo (BE off normalizes to (0, 'off') so a V1
    strategy stored as breakeven_at_r=0.5/mode='off' dedupes correctly)."""
    be = float(p.get("breakeven_at_r") or 0.0)
    mode = str(p.get("breakeven_mode") or "off")
    if be <= 0.0 or mode == "off":
        be, mode = 0.0, "off"
    fvg = p.get("fvg_min_size_ticks")
    return (
        round(float(p.get("risk_reward_ratio") or 2.0), 4),
        round(be, 4), mode,
        int(fvg) if fvg is not None else None,
    )


def _v1_params(strat: dict, use_fvg: bool) -> dict:
    """The strategy's CURRENT params as one grid combo — the V1 baseline. A
    V1 breakeven_mode of 'structure' is preserved as-is (the grid only offers
    off/'r', but the baseline must be what actually runs today)."""
    be = float(strat.get("breakeven_at_r") or 0.0)
    mode = str(strat.get("breakeven_mode") or "off")
    if be <= 0.0 or mode == "off":
        be, mode = 0.0, "off"
    p = {
        "risk_reward_ratio": float(strat.get("risk_reward_ratio") or 2.0),
        "breakeven_at_r": be,
        "breakeven_mode": mode,
    }
    if use_fvg:
        p["fvg_min_size_ticks"] = int(strat.get("fvg_min_size_ticks") or 4)
    return p


def _uses_fvg(job: dict) -> bool:
    """Whether the FVG-size axis is meaningful for this strategy. The v1 ICT
    engine path calls detect_fvgs(min_size_ticks=config.fvg_min_size_ticks)
    on every timeframe unconditionally, so the axis is live for every real
    strategy UNLESS it opted into the v2 setup engine (rule_tree.engine_version
    == 'v2') and its rule_tree never mentions FVGs. Synthetic pulse strategies
    never touch FVGs — axis skipped."""
    if job["synthetic"]:
        return False
    rt = job["strat"].get("rule_tree") or {}
    ev = str(rt.get("engine_version", "v1") or "v1").strip().lower()
    if ev != "v2":
        return True
    blob = json.dumps(rt).lower()
    return ("fvg" in blob) or ("fair_value_gap" in blob)


def _prune_even(combos: list[dict], budget: int) -> list[dict]:
    """Deterministic evenly-spaced subsample preserving grid order, so pruning
    keeps coverage across every axis instead of truncating one corner."""
    n = len(combos)
    if n <= budget:
        return list(combos)
    if budget == 1:
        return [combos[0]]
    idxs: list[int] = []
    for i in range(budget):
        j = round(i * (n - 1) / (budget - 1))
        if not idxs or j > idxs[-1]:
            idxs.append(j)
    return [combos[j] for j in idxs]


def build_grid(strat: dict, use_fvg: bool, max_combos: int) -> tuple[list[dict], int, bool]:
    """Full axis product, pruned to max_combos, with the V1 combo guaranteed
    present. Returns (combos, v1_index, was_pruned)."""
    grid: list[dict] = []
    for rr in RR_AXIS:
        for be, mode in BE_AXIS:
            base = {"risk_reward_ratio": rr, "breakeven_at_r": be, "breakeven_mode": mode}
            if use_fvg:
                for f in FVG_AXIS:
                    grid.append({**base, "fvg_min_size_ticks": f})
            else:
                grid.append(dict(base))

    budget = max(2, int(max_combos))
    was_pruned = len(grid) > budget
    combos = _prune_even(grid, budget)

    v1 = _v1_params(strat, use_fvg)
    keyed = {_combo_key(c): i for i, c in enumerate(combos)}
    v1_key = _combo_key(v1)
    if v1_key in keyed:
        v1_idx = keyed[v1_key]
        combos[v1_idx] = v1  # keep V1's exact values (e.g. mode 'structure')
    else:
        if len(combos) >= budget:
            combos = combos[:-1]  # stay within budget; V1 always makes the cut
        combos.append(v1)
        v1_idx = len(combos) - 1
    return combos, v1_idx, was_pruned


def _fmt_be(p: dict) -> str:
    be = float(p.get("breakeven_at_r") or 0.0)
    mode = str(p.get("breakeven_mode") or "off")
    if be <= 0.0 or mode == "off":
        return "off"
    return f"{be:g}R" + ("@structure" if mode == "structure" else "")


def _param_str(p: dict) -> str:
    s = f"rr={float(p['risk_reward_ratio']):g} be={_fmt_be(p)}"
    if "fvg_min_size_ticks" in p:
        s += f" fvgmin={int(p['fvg_min_size_ticks'])}"
    return s


def _param_diff(v1p: dict, v2p: dict) -> str:
    parts = []
    a, b = float(v1p["risk_reward_ratio"]), float(v2p["risk_reward_ratio"])
    if a != b:
        parts.append(f"rr {a:g}→{b:g}")
    ab, bb = _fmt_be(v1p), _fmt_be(v2p)
    if ab != bb:
        parts.append(f"BE {ab}→{bb}")
    af, bf = v1p.get("fvg_min_size_ticks"), v2p.get("fvg_min_size_ticks")
    if af is not None and bf is not None and int(af) != int(bf):
        parts.append(f"fvgmin {int(af)}→{int(bf)}")
    return ", ".join(parts) if parts else "(same as V1)"


# ─────────────────────────────────────────────────────────────────────────────
# Config builders (shared by pool workers and the main-process OOS legs)
# ─────────────────────────────────────────────────────────────────────────────

def _real_strategy_config(strat: dict, params: dict, inst: str) -> StrategyConfig:
    """MIRRORS opt_worker.run_combo's StrategyConfig construction exactly, so
    the OOS showdown runs the same config the train ranking ran — the only
    intended delta between legs is the window and the slippage."""
    config = StrategyConfig(
        name=strat["name"], instruments=strat.get("instruments") or [inst],
        primary_timeframe=params.get("primary_timeframe", strat.get("primary_timeframe") or "15m"),
        execution_timeframe=params.get("execution_timeframe", strat.get("execution_timeframe") or "1m"),
        higher_timeframes=strat.get("higher_timeframes") or [],
        risk_reward_ratio=float(params.get("risk_reward_ratio", strat.get("risk_reward_ratio") or 2.0)),
        stop_loss_type=params.get("stop_loss_type", strat.get("stop_loss_type") or "structure"),
        stop_loss_ticks=int(params.get("stop_loss_ticks", strat.get("stop_loss_ticks") or 8)),
        max_contracts=strat.get("max_contracts") or 1,
        session_filters=strat.get("session_filters") or [],
        fvg_min_size_ticks=int(params.get("fvg_min_size_ticks", strat.get("fvg_min_size_ticks") or 4)),
        fvg_max_size_ticks=strat.get("fvg_max_size_ticks"),
        max_daily_loss=strat.get("max_daily_loss"),
        max_trades_per_day=strat.get("max_trades_per_day"),
        use_rsi_filter=bool((strat.get("rule_tree") or {}).get("use_rsi_filter", False)),
        use_vwap_filter=bool((strat.get("rule_tree") or {}).get("use_vwap_filter", False)),
    )
    config.rule_tree = strat.get("rule_tree") or {}  # carries engine_version v1/v2
    return config


def _synthetic_strategy_config(params: dict, name: str, inst: str) -> StrategyConfig:
    """Same shape the Phase-A harness uses for the pulse fixture."""
    return StrategyConfig(
        name=f"{name} [wf]", instruments=[inst],
        primary_timeframe="1m", execution_timeframe="1m",
        higher_timeframes=["15m"],
        risk_reward_ratio=float(params.get("risk_reward_ratio") or 2.0),
        max_contracts=10,
    )


def _bt_config(inst: str, start: datetime, end: datetime, primary_tf: str,
               all_tfs: list[str], params: dict, strat: dict,
               slippage_ticks: int) -> BacktestConfig:
    """Same capital/commission/breakeven handling as opt_worker.run_combo;
    only slippage_ticks is parameterized (2 on both OOS legs)."""
    _be = params.get("breakeven_at_r", strat.get("breakeven_at_r"))
    _mode = params.get("breakeven_mode", strat.get("breakeven_mode")) or "off"
    return BacktestConfig(
        instrument=inst, start_date=start, end_date=end,
        primary_timeframe=primary_tf, all_timeframes=all_tfs,
        initial_capital=100_000, commission_per_side=2.50,
        slippage_ticks=int(slippage_ticks),
        breakeven_at_r=float(_be if _be is not None else 0.0),
        breakeven_mode=str(_mode),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pool workers (must be module-level: spawn context pickles by reference)
# ─────────────────────────────────────────────────────────────────────────────

def _pool_init(df_records, instrument, base_tf, tfs):
    """Child-process initializer: quiet the engine's DEBUG/INFO firehose, then
    delegate to the REAL optimizer initializer (builds the shared DataHandler
    once per process — resample once, not per combo)."""
    try:
        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    except Exception:
        pass
    opt_worker.init_worker(df_records, instrument, base_tf, tfs)


def _run_synthetic_combo(idx, params, name, inst, signal_every, stop_ticks,
                         start_date, end_date, slippage_ticks):
    """Synthetic-mode analogue of opt_worker.run_combo: same per-process
    DataHandler, same result-dict shape, but a SyntheticPulseStrategy instead
    of ICTStrategy (the pulse fixture never generates trades under the ICT
    entry logic). Never raises — errors ride back in the result."""
    try:
        dh = opt_worker._WORKER["dh"].unfiltered_copy()
        cfg = _synthetic_strategy_config(params, name, inst)
        strategy = SyntheticPulseStrategy(
            cfg, instrument=inst, signal_every=signal_every, stop_ticks=stop_ticks)
        bt = _bt_config(inst, start_date, end_date, "1m", ["1m", "15m"],
                        params, {"breakeven_at_r": 0.0, "breakeven_mode": "off"},
                        slippage_ticks)
        m = BacktestRunner(strategy, dh, bt).run()
        return idx, {"params": params, **opt_worker._metrics_dict(m)}
    except Exception as e:
        import traceback
        return idx, {
            "params": params, "net_profit": 0, "profit_factor": 0, "win_rate": 0,
            "effective_win_rate": 0, "max_drawdown": 0, "total_trades": 0,
            "sharpe_ratio": 0,
            "_error": f"{type(e).__name__}: {e}", "_tb": traceback.format_exc()[-600:],
        }


# ─────────────────────────────────────────────────────────────────────────────
# OOS showdown (main process — both legs at the SAME harsher slippage)
# ─────────────────────────────────────────────────────────────────────────────

def _eval_oos(job: dict, params: dict, dh: DataHandler, oos_start: datetime,
              oos_end: datetime, slippage_ticks: int) -> dict:
    """One out-of-sample evaluation. Fresh strategy instance + a cheap
    unfiltered_copy() of the prebuilt handler per run (filter_date_range trims
    destructively — two windows must never share a handler)."""
    if job["synthetic"]:
        cfg = _synthetic_strategy_config(params, job["name"], job["inst"])
        strategy = SyntheticPulseStrategy(
            cfg, instrument=job["inst"],
            signal_every=job["spec"]["signal_every"],
            stop_ticks=job["spec"]["stop_ticks"])
        primary_tf, all_tfs = "1m", ["1m", "15m"]
    else:
        from app.engines.backtest_engine.ict_strategy import ICTStrategy
        cfg = _real_strategy_config(job["strat"], params, job["inst"])
        primary_tf = cfg.primary_timeframe
        all_tfs = list(set([cfg.primary_timeframe, cfg.execution_timeframe]
                           + (cfg.higher_timeframes or [])))
        strategy = ICTStrategy(cfg, instrument=job["inst"])
    bt = _bt_config(job["inst"], oos_start, oos_end, primary_tf, all_tfs,
                    params, job["strat"], slippage_ticks)
    m = BacktestRunner(strategy, dh.unfiltered_copy(), bt).run()
    return opt_worker._metrics_dict(m)


# ─────────────────────────────────────────────────────────────────────────────
# Incremental output (header first; one row appended after EACH strategy)
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_cell(key: str, v) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, float) and math.isnan(v):
        return ""
    if isinstance(v, float) and math.isinf(v):
        return "inf"
    if key.endswith("_trades"):
        return str(int(v))
    if key.endswith("_wr_pct") or key.endswith("_dd_pct"):
        return f"{v:.1f}"
    if key.endswith("_net_usd"):
        return f"{v:.2f}"
    return f"{v:.2f}"


MD_ORDER = [
    "v1_oos_trades", "v1_oos_wr_pct", "v1_oos_pf", "v1_oos_dd_pct", "v1_oos_net_usd",
    "v2_oos_trades", "v2_oos_wr_pct", "v2_oos_pf", "v2_oos_dd_pct", "v2_oos_net_usd",
    "v2_train_trades", "v2_train_pf",
]


class IncrementalWriter:
    """Writes headers up front, then appends one row per strategy and flushes,
    so a killed run leaves a valid partial CSV/MD behind."""

    def __init__(self, out_dir: Path, meta: dict):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = out_dir / CSV_NAME
        self.md_path = out_dir / MD_NAME
        with open(self.csv_path, "w", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FIELDNAMES).writeheader()
        lines = [
            "# Strategy V2 — walk-forward optimization (V1 params vs V2 candidate, out-of-sample)",
            "",
            (f"_Generated {meta['generated_at']} · window {meta['start']} → {meta['end']} · "
             f"walk-forward split {meta['split']} (train head / last {meta['oos_fraction']:.0%} OOS) · "
             f"train ranking: profit factor at optimizer slippage (1 tick/side), min {MIN_TRAIN_TRADES} "
             f"train trades, top {TOP_N_TRAIN} advance · OOS showdown: BOTH legs at "
             f"{meta['oos_slippage_ticks']} ticks/side slippage, V2 candidate needs ≥{MIN_OOS_TRADES} "
             f"OOS trades · grid ≤{meta['max_combos']} combos (V1 params always included) · "
             f"workers {meta['workers']} · data: {meta['data_source']}._"),
            "",
            ("Verdicts: V2_BETTER = the best OOS combo beats the V1 params out-of-sample; "
             "V1_HOLDS = no grid combo beat V1 on the holdout; INSUFFICIENT_TRADES = either "
             "leg lacked the minimum OOS trades for an honest call. V2 columns are blank when "
             "no combo met the trade minimums. Rows append incrementally — a partial file "
             "means the run was killed mid-book."),
            "",
            "| Strategy | Inst | Verdict | V1 Tr | V1 WR% | V1 PF | V1 DD% | V1 Net$ | V2 Tr | V2 WR% | V2 PF | V2 DD% | V2 Net$ | V2 train Tr | V2 train PF | Param diff |",
            "|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|",
        ]
        self.md_path.write_text("\n".join(lines) + "\n")

    def append(self, row: dict) -> None:
        with open(self.csv_path, "a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FIELDNAMES).writerow(
                {k: _fmt_cell(k, row.get(k)) for k in FIELDNAMES})
        cells = [row["strategy"], row["instrument"], row["verdict"]]
        cells += [_fmt_cell(k, row.get(k)) for k in MD_ORDER]
        cells.append(row.get("param_diff") or "")
        with open(self.md_path, "a") as fh:
            fh.write("| " + " | ".join(cells) + " |\n")


# ─────────────────────────────────────────────────────────────────────────────
# Per-strategy pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _metric_cols(prefix: str, m: Optional[dict]) -> dict:
    if m is None:
        return {f"{prefix}_trades": None, f"{prefix}_wr_pct": None, f"{prefix}_pf": None,
                f"{prefix}_dd_pct": None, f"{prefix}_net_usd": None}
    return {
        f"{prefix}_trades": int(m.get("total_trades") or 0),
        f"{prefix}_wr_pct": float(m.get("win_rate") or 0.0) * 100.0,
        f"{prefix}_pf": float(m.get("profit_factor") or 0.0),
        f"{prefix}_dd_pct": float(m.get("max_drawdown") or 0.0),
        f"{prefix}_net_usd": float(m.get("net_profit") or 0.0),
    }


def _pf_of(m: dict) -> float:
    pf = float(m.get("profit_factor") or 0.0)
    return 0.0 if math.isnan(pf) else pf


def process_job(job: dict, args: argparse.Namespace, split: datetime,
                train_end: datetime, writer: IncrementalWriter) -> None:
    name, inst = job["name"], job["inst"]
    use_fvg = _uses_fvg(job)
    combos, v1_idx, was_pruned = build_grid(job["strat"], use_fvg, args.max_combos)
    n = len(combos)
    logger.info(
        f"[wf] {name} ({inst}): {n} combos "
        f"(axes rr×{len(RR_AXIS)} be×{len(BE_AXIS)}"
        + (f" fvgmin×{len(FVG_AXIS)}" if use_fvg else " — no FVG axis")
        + (", pruned" if was_pruned else "")
        + f"; V1 combo #{v1_idx}: {_param_str(combos[v1_idx])}) | "
        f"train [{args.start} → {split:%Y-%m-%d %H:%M}) / OOS [{split:%Y-%m-%d %H:%M} → {args.end}]"
    )

    # ── Train phase: every combo on the train head via the REAL opt worker ──
    records = job["df"].to_dict("list")  # same wire format the route uses
    train_results: list[Optional[dict]] = [None] * n
    workers = max(1, min(args.workers, n))
    ctx = mp.get_context("spawn")  # parity with the live optimizer pool
    with cf.ProcessPoolExecutor(
            max_workers=workers, mp_context=ctx,
            initializer=_pool_init,
            initargs=(records, inst, job["base_tf"], list(job["tfs"]))) as pool:
        futs = {}
        for i, p in enumerate(combos):
            if job["synthetic"]:
                fut = pool.submit(
                    _run_synthetic_combo, i, p, name, inst,
                    job["spec"]["signal_every"], job["spec"]["stop_ticks"],
                    args.start_dt, train_end, 1)
            else:
                # oos_fraction=0.0 → one full run over [start, train_end]:
                # this IS the train leg, scored by the live worker.
                fut = pool.submit(opt_worker.run_combo, i, p, job["strat"],
                                  args.start_dt, train_end, 0.0)
            futs[fut] = i
        done = failed = 0
        for fut in cf.as_completed(futs):
            idx, res = fut.result()
            train_results[idx] = res
            done += 1
            if res.get("_error"):
                failed += 1
                logger.warning(f"[wf] {name}: train {done}/{n} combo#{idx} "
                               f"FAILED — {res['_error']}")
            else:
                logger.info(
                    f"[wf] {name}: train {done}/{n} combo#{idx} "
                    f"[{_param_str(combos[idx])}] pf={_pf_of(res):.2f} "
                    f"trades={int(res.get('total_trades') or 0)} "
                    f"net=${float(res.get('net_profit') or 0.0):,.0f}")
    if failed:
        logger.warning(f"[wf] {name}: {failed}/{n} train combos failed")

    # ── Rank on TRAIN ONLY: top 5 by train PF, min 20 train trades ──────────
    scored = []
    for i, res in enumerate(train_results):
        if not res or res.get("_error"):
            continue
        if int(res.get("total_trades") or 0) < MIN_TRAIN_TRADES:
            continue
        scored.append((_pf_of(res), float(res.get("net_profit") or 0.0), i))
    scored.sort(reverse=True)
    top = [i for _, _, i in scored[:TOP_N_TRAIN]]
    if top:
        logger.info(f"[wf] {name}: train top-{len(top)} → " + "; ".join(
            f"#{i} [{_param_str(combos[i])}] pf={pf:.2f}" for pf, _, i in scored[:TOP_N_TRAIN]))
    else:
        logger.warning(f"[wf] {name}: NO combo met min {MIN_TRAIN_TRADES} train "
                       f"trades — OOS showdown runs V1 params only")

    # ── OOS showdown: top-5 + V1 combo, BOTH at the harsher slippage ────────
    showdown: list[int] = []
    for i in top + [v1_idx]:
        if i not in showdown:
            showdown.append(i)
    dh = DataHandler(instrument=inst, base_timeframe=job["base_tf"])
    dh.load_from_dataframe(job["df"])
    dh.build_timeframes(list(job["tfs"]))
    oos: dict[int, dict] = {}
    for i in showdown:
        oos[i] = _eval_oos(job, combos[i], dh, split, args.end_dt,
                           args.oos_slippage_ticks)
        logger.info(
            f"[wf] {name}: OOS combo#{i}{' (V1 params)' if i == v1_idx else ''} "
            f"[{_param_str(combos[i])}] pf={_pf_of(oos[i]):.2f} "
            f"trades={int(oos[i].get('total_trades') or 0)} "
            f"net=${float(oos[i].get('net_profit') or 0.0):,.0f} "
            f"@ {args.oos_slippage_ticks}-tick slippage")

    v1m = oos[v1_idx]
    v1_trades = int(v1m.get("total_trades") or 0)
    eligible = [i for i in showdown
                if int(oos[i].get("total_trades") or 0) >= MIN_OOS_TRADES]
    cand_idx = max(eligible, key=lambda i: (_pf_of(oos[i]),
                                            float(oos[i].get("net_profit") or 0.0))) \
        if eligible else None

    if cand_idx is None or v1_trades < MIN_OOS_TRADES:
        # Either no combo produced enough OOS trades to pick a candidate, or
        # the V1 leg itself is too thin for an honest comparison.
        verdict = "INSUFFICIENT_TRADES"
    elif cand_idx == v1_idx:
        verdict = "V1_HOLDS"
    elif _pf_of(oos[cand_idx]) > _pf_of(v1m):
        verdict = "V2_BETTER"
    else:
        verdict = "V1_HOLDS"

    cand_m = oos.get(cand_idx) if cand_idx is not None else None
    cand_train = train_results[cand_idx] if cand_idx is not None else None
    row = {
        "strategy": name, "instrument": inst, "verdict": verdict,
        **_metric_cols("v1_oos", v1m),
        **_metric_cols("v2_oos", cand_m),
        "v2_train_trades": (int(cand_train.get("total_trades") or 0)
                            if cand_train and not cand_train.get("_error") else None),
        "v2_train_pf": (_pf_of(cand_train)
                        if cand_train and not cand_train.get("_error") else None),
        "param_diff": ("" if cand_idx is None
                       else "(same as V1)" if cand_idx == v1_idx
                       else _param_diff(combos[v1_idx], combos[cand_idx])),
        "v2_params_json": (json.dumps(combos[cand_idx], sort_keys=True)
                           if cand_idx is not None else ""),
    }
    writer.append(row)
    logger.info(
        f"[wf] {name}: verdict={verdict} | V1 OOS pf={_pf_of(v1m):.2f}/"
        f"{v1_trades}tr vs V2 "
        + (f"pf={_pf_of(cand_m):.2f}/{int(cand_m.get('total_trades') or 0)}tr "
           f"[{row['param_diff']}]" if cand_m else "(no candidate)")
        + " — row appended")


# ─────────────────────────────────────────────────────────────────────────────
# Job loading — synthetic book (no DB) and real strategies (DB + candle cache)
# ─────────────────────────────────────────────────────────────────────────────

def _synthetic_jobs(args: argparse.Namespace) -> list[dict]:
    names = args.strategy_names or [n for n, _, _ in SYNTHETIC_BOOK]
    jobs = []
    for raw in names:
        name, inst, spec = _spec_for_name(raw)
        # Per-instrument seed offset — same convention as the Phase-A harness
        # so both scripts smoke against identical tapes for a given --seed.
        bars = generate_synthetic_bars(
            args.start_dt, args.end_dt,
            seed=args.seed + (zlib.crc32(inst.encode("utf-8")) % 997))
        strat = {
            "name": name, "instruments": [inst],
            "risk_reward_ratio": float(spec["rr"]),
            "breakeven_at_r": 0.0, "breakeven_mode": "off",
            "fvg_min_size_ticks": 4, "rule_tree": {},
        }
        jobs.append({"name": name, "inst": inst, "synthetic": True,
                     "spec": spec, "strat": strat, "df": bars,
                     "base_tf": "1m", "tfs": ["1m", "15m"]})
    return jobs


async def _load_db_jobs(args: argparse.Namespace) -> list[dict]:
    # Route-pattern imports kept inside the function so --synthetic never
    # touches DB modules (same as the Phase-A harness / routes/backtests.py).
    from sqlalchemy import select, cast, String
    import app.models  # noqa: F401 — full mapper registry so relationships resolve
    from app.database import async_session_factory
    from app.models.strategy import Strategy
    from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data

    logger.info("[wf] connecting to db …")
    async with async_session_factory() as session:
        # status is stored UPPERCASE in prod while the enum value is lowercase
        # — compare as text, the codebase convention (see the harness).
        result = await session.execute(
            select(Strategy).where(cast(Strategy.status, String) == "ACTIVE"))
        strategies = result.scalars().all()
    logger.info(f"[wf] loaded {len(strategies)} ACTIVE strategies from db")

    jobs: list[dict] = []
    seen: set[str] = set()
    cache: dict[tuple, object] = {}
    for s in strategies:
        if args.strategy_names and s.name not in args.strategy_names:
            continue
        if s.name in seen:
            continue  # one job per unique NAME (per-user duplicate definitions)
        inst = next((str(i).upper() for i in (s.instruments or [])
                     if str(i).upper() in TICK_SIZES), None)
        if inst is None:
            continue  # options / stock strategies — not this driver's lane
        seen.add(s.name)
        exec_tf = s.execution_timeframe or "1m"
        strat = {
            "name": s.name, "instruments": s.instruments,
            "primary_timeframe": s.primary_timeframe,
            "execution_timeframe": exec_tf,
            "higher_timeframes": s.higher_timeframes,
            "risk_reward_ratio": s.risk_reward_ratio,
            "stop_loss_type": s.stop_loss_type,
            "stop_loss_ticks": s.stop_loss_ticks,
            "max_contracts": s.max_contracts,
            "session_filters": s.session_filters,
            "fvg_min_size_ticks": s.fvg_min_size_ticks,
            "fvg_max_size_ticks": s.fvg_max_size_ticks,
            "max_daily_loss": s.max_daily_loss,
            "max_trades_per_day": s.max_trades_per_day,
            "breakeven_at_r": getattr(s, "breakeven_at_r", None),
            "breakeven_mode": getattr(s, "breakeven_mode", None),
            "rule_tree": s.rule_tree or {},
        }
        key = (inst, exec_tf)
        try:
            if key not in cache:
                logger.info(f"[wf] fetching {inst} {exec_tf} bars "
                            f"{args.start} → {args.end} …")
                cache[key] = await fetch_futures_data(
                    instrument=inst, start_date=args.start_dt,
                    end_date=args.end_dt, interval=exec_tf, use_polygon=True)
            df = cache[key]
        except Exception as exc:
            logger.error(f"[wf] data fetch failed for {inst} {exec_tf}: "
                         f"{type(exc).__name__}: {exc} — skipping {s.name!r}")
            continue
        if df is None or getattr(df, "empty", True):
            logger.warning(f"[wf] no data for {inst} {exec_tf} — skipping {s.name!r}")
            continue
        tfs = {s.primary_timeframe or "15m", exec_tf}
        tfs.update(s.higher_timeframes or [])
        jobs.append({"name": s.name, "inst": inst, "synthetic": False,
                     "spec": None, "strat": strat, "df": df.reset_index(),
                     "base_tf": exec_tf, "tfs": sorted(tfs)})
    if not jobs:
        logger.warning("[wf] no matching active futures strategies")
    return jobs


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Walk-forward optimize every active futures strategy and "
                    "compare against its V1 params out-of-sample "
                    "(see module docstring).")
    ap.add_argument("--strategy-name", action="append", dest="strategy_names",
                    metavar="NAME", help="strategy name to run (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="run every active futures strategy "
                         "(implied by --synthetic when no names are given)")
    ap.add_argument("--start", default="2026-04-01", help="window start YYYY-MM-DD")
    ap.add_argument("--end", default="2026-05-01", help="window end YYYY-MM-DD")
    ap.add_argument("--oos-fraction", type=float, default=0.3,
                    help="fraction of the window held out-of-sample at the END "
                         "(default 0.3; must be >0 — this is a walk-forward driver)")
    ap.add_argument("--max-combos", type=int, default=48,
                    help="cap on grid combos per strategy incl. the V1 combo (default 48)")
    ap.add_argument("--workers", type=int, default=3,
                    help="ProcessPool cap for the train phase (default 3)")
    ap.add_argument("--oos-slippage-ticks", type=int, default=2,
                    help="slippage on BOTH OOS legs — V1 params included (default 2)")
    ap.add_argument("--synthetic", action="store_true",
                    help="run on deterministic generated bars — no DB required")
    ap.add_argument("--seed", type=int, default=42, help="synthetic bar seed (default 42)")
    ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                    help=f"where to write {CSV_NAME} / {MD_NAME}")
    ap.add_argument("--verbose", action="store_true", help="keep engine DEBUG logging")
    args = ap.parse_args(argv)

    if not args.synthetic and not args.all and not args.strategy_names:
        ap.error("provide --strategy-name or --all (or --synthetic)")
    try:
        args.start_dt = datetime.strptime(args.start, "%Y-%m-%d")
        args.end_dt = datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError as exc:
        ap.error(f"bad date: {exc}")
    if args.end_dt <= args.start_dt:
        ap.error("--end must be after --start")
    if not (0.0 < args.oos_fraction < 0.9):
        ap.error("--oos-fraction must be in (0, 0.9) — walk-forward needs a holdout")
    if args.max_combos < 2:
        ap.error("--max-combos must be >= 2")
    if args.workers < 1:
        ap.error("--workers must be >= 1")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if not args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="INFO")

    split = split_walkforward(args.start_dt, args.end_dt, args.oos_fraction)
    train_end = split - TRAIN_END_EPSILON

    if args.synthetic:
        jobs = _synthetic_jobs(args)
        data_source = f"synthetic deterministic bars (seed {args.seed})"
    else:
        import asyncio
        jobs = asyncio.run(_load_db_jobs(args))
        data_source = "live candle cache / Polygon futures"

    if not jobs:
        logger.error("[wf] nothing to run")
        return 1

    meta = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "start": args.start, "end": args.end,
        "split": split.strftime("%Y-%m-%d %H:%M"),
        "oos_fraction": args.oos_fraction,
        "oos_slippage_ticks": args.oos_slippage_ticks,
        "max_combos": args.max_combos, "workers": args.workers,
        "data_source": data_source,
    }
    writer = IncrementalWriter(Path(args.output_dir), meta)
    logger.info(f"[wf] {len(jobs)} strategies queued; incremental output → "
                f"{writer.csv_path} / {writer.md_path}")

    ok = 0
    for pos, job in enumerate(jobs, start=1):
        logger.info(f"[wf] ── strategy {pos}/{len(jobs)}: {job['name']} ──")
        try:
            process_job(job, args, split, train_end, writer)
            ok += 1
        except Exception as exc:
            # One broken strategy must not sink the batch — partials survive.
            logger.error(f"[wf] {job['name']!r} failed: "
                         f"{type(exc).__name__}: {exc}")
            continue

    logger.info(f"[wf] done: {ok}/{len(jobs)} strategies written to "
                f"{writer.csv_path} and {writer.md_path}")
    print(writer.md_path.read_text())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
