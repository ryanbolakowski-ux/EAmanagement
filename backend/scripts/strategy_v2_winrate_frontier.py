"""
Strategy V2 WIN-RATE FRONTIER — Phase-C batch driver (v2-redesign).

Design (per owner direction 2026-07): win rate is pursued through SELECTIVITY
(fewer, better trades — no-trade days are acceptable by design) and through
STRUCTURE-BASED TARGETS, NOT by shrinking a fixed R:R target. There is no
risk_reward_ratio axis in this grid: every combo keeps the strategy's current
rr, which the engine only uses as the last-resort fallback target and the
range-mode sanity floor.

GRID AXES (only knobs that are ACTUALLY wired in the engine):

  take_profit_mode ∈ {auto, range}
      The ONLY two target modes the engine supports (StrategyConfig
      .take_profit_mode, validated 'auto'|'range' by the AI-builder schema;
      RANGE-TP-V1 in ict_strategy._compute_take_profit). 'auto' IS the
      structure hierarchy: (1) next swing high/low on the primary TF minus a
      2-tick buffer when ≥ 1R away, (2) an unfilled HTF FVG CE past that
      swing when ≥ 1.5R, (3) classic R:R from config as the fallback.
      'range' targets the far extreme of the last-60-bar dealing range
      (≥ 1R floor, otherwise falls through to the auto hierarchy). NOTE:
      opt_worker.run_combo does NOT plumb take_profit_mode from params —
      this driver therefore runs its own pool worker (same DataHandler
      init, same metrics) so the axis is real.

  market activity gate ∈ {on, off}          (--gate both|on|off)
      DISCOVERED MECHANISM: there is NO config field, DB column, or env
      kill-switch for the gate. ICTStrategy.on_bar calls
      _futures_activity_gate → evaluate_activity_gate unconditionally for
      futures symbols; the only env knob, FUTURES_GATE_GO_THRESHOLD, is
      clamped to (0,1) with 0 explicitly rejected — so there is no
      threshold-based "off". Gate-OFF legs therefore monkeypatch
      market_activity_gate.evaluate_activity_gate to return None — the
      module's own documented ABSTAIN/fail-open path ("callers MUST treat
      None as no opinion") — which restores the legacy session-window
      behavior. The patch works because _futures_activity_gate re-imports
      the symbol from the module on EVERY call; it is applied around each
      backtest run and restored in a finally block (process-local, never
      touches engine files). Gate-ON legs use the engine default futures
      threshold (0.40, or FUTURES_GATE_GO_THRESHOLD if set).
      INTERACTION CAVEAT: when the gate renders a verdict on a bar, the
      hard session-window block is BYPASSED (_gate_governs in on_bar), so
      the session axis below only bites on gate-off legs and on
      gate-abstain bars. Rows must be read with that in mind.

  session_filters ∈ {current set, primary session only}
      Selectivity via time-of-day narrowing. "Primary session" is defined
      as the FIRST entry of the strategy's session_filters list
      (declaration order; documented assumption). Axis collapses when the
      strategy has fewer than two session filters. Known session names:
      NY, NY_AM, NY_PM, LONDON, LONDON_CLOSE, ASIA (indicators.is_in_session).

  confluence strictness ∈ {current, strict}
      strict = use_rsi_filter AND use_vwap_filter both ON (the only
      confluence-veto knobs that are config/rule_tree-exposed). The
      displacement check and the liquidity-sweep requirement are HARD-CODED
      on in the v1 engine path — the only related knob,
      rule_tree.bypass_bias_gates, LOOSENS them and is therefore not a
      win-rate lever. Axis collapses when both filters are already on.

  fvg_min_size_ticks ∈ {4, 6}
      Larger-gap selectivity; only when the strategy's engine path consumes
      FVGs (same _uses_fvg rule as the Phase-B driver).

  breakeven_at_r ∈ {off, 0.5R (mode 'r')}
      Runner-level control pair, kept from the original Phase-C spec.

The strategy's CURRENT config is ALWAYS included as the labeled V1-baseline
control leg (its own tp mode, sessions, filters, BE, gate ON — live
behavior). The grid is pruned deterministically (evenly spaced) to
--max-combos with the V1 combo guaranteed present.

THREE-WAY OUTCOME ACCOUNTING (the key feature):
  Each leg's completed-trade list is post-processed. A trade is BREAKEVEN if
      exit_reason == 'breakeven'   (engine-tagged stop-moved-to-entry exit)
    OR
      |net_pnl| <= max(2 × round-trip commission, 0.10 × initial risk $)
  where round-trip commission = commission_per_side × 2 × contracts (the
  trade's booked commission field) and initial risk $ =
  |entry − stop| / tick_size × tick_value × contracts using the TRADED
  instrument's tick math (micro auto-substitution respected). This mirrors
  the live resolver's spirit (account_signals outcome 'breakeven' = scratch
  at entry, "not a loss") with a tolerance for near-zero scratches.
  Reported per leg, matching metrics.win_rate_stats / GET /signals/stats:
      raw WR       = (wins + BE) / all trades      (BE counts as a non-loss,
                                                    same as backtest is_winner)
      effective WR = wins / (wins + losses)        (BE excluded entirely)
      BE count
  Trades/week = trades / (window_days / 7) — the selectivity cost, visible.

PIPELINE (per strategy):
  1. TRAIN on the head window (walk-forward split identical to the live
     optimizer: split_walkforward + TRAIN_END_EPSILON). Every combo runs in
     a spawn ProcessPool via this module's own worker (opt_worker.init_worker
     DataHandler, optimizer-parity 1-tick slippage). Run with
     V2_FAST_BACKTEST=0 so the stable engine path is used.
  2. ADVANCE the top 8 combos by TRAIN effective WR among combos with train
     PF >= --min-pf and >= 20 train trades. If NONE qualify, the PF bar
     relaxes to 1.0, then drops entirely; rows advanced under a relaxed bar
     are flagged TRAIN_RELAXED (a frontier with a caveat beats an empty
     table). Only the >= 20-train-trades floor is never relaxed.
  3. OOS SHOWDOWN in-process: finalists + the V1 baseline, each run TWICE:
       '2t' = 2 ticks/side slippage  (today's market entries, delayed data)
       '1t' = 1 tick/side slippage   (near-perfect-fill future: limit
                                      entries at the FVG midpoint + a
                                      real-time feed)
     Both slippage scenarios are reported side by side for every row.
  4. SELECTION — MAX-HONEST-WR pick: highest OOS(2t) effective WR subject to
     OOS(2t) PF >= --min-pf (default 2.0) and >= 6 OOS trades (selectivity
     shrinks samples). Rows with < 10 OOS(2t) trades are flagged LOW_SAMPLE.
     The pick is flagged MAX_HONEST_WR; the V1 row is role V1_BASELINE for
     contrast. No row flagged = no combo met the bar for that strategy.

OUTPUT: docs/v2/strategy-v2-winrate-frontier.{csv,md} (NEW filenames only) —
header first, rows APPENDED after each strategy so partials survive a kill.

Run (real data — inside the backend container; NEVER point at prod from a
worktree; always with V2_FAST_BACKTEST=0):
    python -m scripts.strategy_v2_winrate_frontier --all \
        --start 2026-04-01 --end 2026-06-13 --workers 3
Run (synthetic deterministic bars — no DB; the smoke path; the tp/gate/
session/filter/FVG axes are INERT for the pulse fixture, so only the BE axis
and the slippage scenarios differentiate synthetic rows — the smoke exercises
the mechanics, not the edge):
    python -m scripts.strategy_v2_winrate_frontier --synthetic --seed 42 --max-combos 8
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import csv
import json
import math
import multiprocessing as mp
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from loguru import logger

from app.engines.backtest_engine.backtest_runner import (
    BacktestRunner, TICK_SIZES, TICK_VALUES,
)
from app.engines.backtest_engine.data_handler import DataHandler

from app.engines.optimization_engine import opt_worker
from app.engines.optimization_engine.opt_worker import (
    TRAIN_END_EPSILON, split_walkforward,
)

# REUSE the Phase-B driver's plumbing: job loading (db + synthetic), config
# builders, pruning, pool initializer, FVG-axis rule. Same structure, new grid.
from scripts.strategy_v2_walkforward import (
    DEFAULT_OUTPUT_DIR, _bt_config, _fmt_be, _pf_of, _pool_init, _prune_even,
    _real_strategy_config, _synthetic_jobs, _synthetic_strategy_config,
    _uses_fvg, _load_db_jobs,
)
from scripts.strategy_v2_harness import SyntheticPulseStrategy

CSV_NAME = "strategy-v2-winrate-frontier.csv"
MD_NAME = "strategy-v2-winrate-frontier.md"

# ── Grid axes (see module docstring; NO risk_reward_ratio axis by design) ───
TPM_AXIS = ["auto", "range"]
BE_AXIS = [(0.0, "off"), (0.5, "r")]
FVG_AXIS = [4, 6]

TRAIN_SLIPPAGE_TICKS = 1          # optimizer parity on the train leg
OOS_SLIPPAGE_SCENARIOS = (        # label, ticks/side — BOTH run per finalist
    ("2t", 2),                    # today's market entries on delayed data
    ("1t", 1),                    # near-perfect-fill future (limit @ FVG mid)
)
SELECT_SCENARIO = "2t"            # selection/flags read this scenario

TOP_N_TRAIN = 8                   # combos advanced by train effective WR
MIN_TRAIN_TRADES = 20             # train floor to rank at all
RELAXED_TRAIN_PF = 1.0            # fallback PF bar when nothing meets --min-pf
MIN_OOS_TRADES = 6                # floor for the MAX-HONEST-WR pick
LOW_SAMPLE_OOS = 10               # below this, row flagged LOW_SAMPLE

# BREAKEVEN threshold (mirrors the live resolver's spirit — documented above)
BE_COMM_MULT = 2.0                # × the trade's round-trip commission
BE_RISK_FRAC = 0.10               # × the trade's initial risk in dollars

FIELDNAMES = [
    "strategy", "instrument", "role", "flags", "gate", "tp_mode", "params",
    "train_trades", "train_pf", "train_eff_wr_pct",
    "oos2t_trades", "oos2t_tpw", "oos2t_raw_wr_pct", "oos2t_eff_wr_pct",
    "oos2t_be", "oos2t_pf", "oos2t_dd_pct", "oos2t_net_usd",
    "oos1t_trades", "oos1t_tpw", "oos1t_raw_wr_pct", "oos1t_eff_wr_pct",
    "oos1t_be", "oos1t_pf", "oos1t_dd_pct", "oos1t_net_usd",
    "params_json",
]


# ─────────────────────────────────────────────────────────────────────────────
# Gate toggle (the discovered mechanism — see module docstring)
# ─────────────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _gate_disabled(off: bool):
    """Gate-OFF legs: monkeypatch evaluate_activity_gate to the ABSTAIN value
    (None), the module's documented fail-open path — legacy session-window
    behavior applies. No engine file is touched; restored in finally. A no-op
    context for gate-ON legs and for the synthetic pulse fixture (which never
    consults the gate)."""
    if not off:
        yield
        return
    from app.engines.strategy_engine import market_activity_gate as _mag
    _orig = _mag.evaluate_activity_gate
    _mag.evaluate_activity_gate = lambda instrument, df, cfg=None: None
    try:
        yield
    finally:
        _mag.evaluate_activity_gate = _orig


# ─────────────────────────────────────────────────────────────────────────────
# Three-way outcome accounting (the key feature)
# ─────────────────────────────────────────────────────────────────────────────

def _classify_trade(t) -> str:
    """win | loss | be for one SimulatedTrade, per the documented threshold:
    BE if exit_reason=='breakeven' OR |net_pnl| <= max(2×round-trip commission,
    0.10 × initial risk $). Tick math uses the TRADED instrument (micro
    auto-substitution respected)."""
    ts = TICK_SIZES.get(t.instrument, 0.25)
    tv = TICK_VALUES.get(t.instrument, 12.50)
    risk_usd = abs(t.entry_price - t.stop_loss) / ts * tv * t.contracts if ts else 0.0
    thresh = max(BE_COMM_MULT * float(t.commission or 0.0), BE_RISK_FRAC * risk_usd)
    if t.exit_reason == "breakeven" or abs(t.net_pnl) <= thresh:
        return "be"
    return "win" if t.net_pnl > 0 else "loss"


def _three_way(trades: list) -> dict:
    """raw WR = (wins+BE)/all (BE = non-loss, matching backtest is_winner and
    the live /signals/stats win_rate); effective WR = wins/(wins+losses), BE
    excluded (matching effective_win_rate everywhere in the book)."""
    wins = losses = be = 0
    for t in trades:
        c = _classify_trade(t)
        if c == "win":
            wins += 1
        elif c == "loss":
            losses += 1
        else:
            be += 1
    n = wins + losses + be
    return {
        "tw_wins": wins, "tw_losses": losses, "tw_be": be, "tw_total": n,
        "tw_raw_wr": ((wins + be) / n) if n else 0.0,
        "tw_eff_wr": (wins / (wins + losses)) if (wins + losses) else 0.0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Parameter grid
# ─────────────────────────────────────────────────────────────────────────────

def _norm_be(p: dict) -> tuple[float, str]:
    be = float(p.get("breakeven_at_r") or 0.0)
    mode = str(p.get("breakeven_mode") or "off")
    if be <= 0.0 or mode == "off":
        return 0.0, "off"
    return round(be, 4), mode


def _combo_key(p: dict) -> tuple:
    be, mode = _norm_be(p)
    sess = tuple(p["session_filters"]) if "session_filters" in p else None
    fvg = p.get("fvg_min_size_ticks")
    return (
        str(p.get("take_profit_mode") or "-"),
        str(p.get("gate") or "on"),
        sess,
        p.get("use_rsi_filter"), p.get("use_vwap_filter"),
        int(fvg) if fvg is not None else None,
        be, mode,
    )


def _axes_for(job: dict, args: argparse.Namespace) -> dict:
    """Which axes are LIVE for this strategy (collapsed axes carry one value
    or are omitted from combo dicts entirely so workers fall back to the
    strategy's own config)."""
    synth = job["synthetic"]
    strat = job["strat"]
    rt = strat.get("rule_tree") or {}
    gate_axis = {"both": ["on", "off"], "on": ["on"], "off": ["off"]}[args.gate]
    cur_sessions = [str(s) for s in (strat.get("session_filters") or [])]
    cur_rsi = bool(rt.get("use_rsi_filter", False))
    cur_vwap = bool(rt.get("use_vwap_filter", False))
    return {
        "tpm": ([None] if synth else list(TPM_AXIS)),
        "gate": gate_axis,
        "sess": ([None] if (synth or len(cur_sessions) < 2)
                 else [cur_sessions, [cur_sessions[0]]]),
        "filt": ([None] if (synth or (cur_rsi and cur_vwap))
                 else [(cur_rsi, cur_vwap), (True, True)]),
        "fvg": (list(FVG_AXIS) if _uses_fvg(job) else [None]),
        "be": list(BE_AXIS),
        "_cur": {"sessions": cur_sessions, "rsi": cur_rsi, "vwap": cur_vwap,
                 "tpm": str(rt.get("take_profit_mode", "auto") or "auto").lower()},
    }


def _v1_combo(job: dict, axes: dict, args: argparse.Namespace) -> dict:
    """The strategy's CURRENT config as one labeled combo — the control leg.
    Gate ON (live behavior) unless the whole run is --gate off. Only keys for
    LIVE axes are set, so collapsed axes stay on the strategy's own config."""
    strat, cur = job["strat"], axes["_cur"]
    be, mode = _norm_be(strat)
    p: dict = {"gate": "off" if args.gate == "off" else "on",
               "breakeven_at_r": be, "breakeven_mode": mode}
    if not job["synthetic"]:
        p["take_profit_mode"] = cur["tpm"]
        if axes["fvg"] != [None]:
            p["fvg_min_size_ticks"] = int(strat.get("fvg_min_size_ticks") or 4)
        if axes["sess"] != [None]:
            p["session_filters"] = list(cur["sessions"])
        if axes["filt"] != [None]:
            p["use_rsi_filter"], p["use_vwap_filter"] = cur["rsi"], cur["vwap"]
    return p


def build_grid(job: dict, args: argparse.Namespace) -> tuple[list[dict], int, bool, dict]:
    """Axis product → deterministic even pruning to --max-combos → V1 combo
    guaranteed present. Returns (combos, v1_index, was_pruned, axes)."""
    axes = _axes_for(job, args)
    grid: list[dict] = []
    for tpm in axes["tpm"]:
        for gate in axes["gate"]:
            for sess in axes["sess"]:
                for filt in axes["filt"]:
                    for fvg in axes["fvg"]:
                        for be, mode in axes["be"]:
                            p: dict = {"gate": gate, "breakeven_at_r": be,
                                       "breakeven_mode": mode}
                            if tpm is not None:
                                p["take_profit_mode"] = tpm
                            if sess is not None:
                                p["session_filters"] = list(sess)
                            if filt is not None:
                                p["use_rsi_filter"], p["use_vwap_filter"] = filt
                            if fvg is not None:
                                p["fvg_min_size_ticks"] = int(fvg)
                            grid.append(p)
    # dedupe (collapsed axes can alias combos)
    seen: dict[tuple, int] = {}
    combos: list[dict] = []
    for p in grid:
        k = _combo_key(p)
        if k not in seen:
            seen[k] = len(combos)
            combos.append(p)

    budget = max(2, int(args.max_combos))
    was_pruned = len(combos) > budget
    combos = _prune_even(combos, budget)

    v1 = _v1_combo(job, axes, args)
    keyed = {_combo_key(c): i for i, c in enumerate(combos)}
    v1_key = _combo_key(v1)
    if v1_key in keyed:
        v1_idx = keyed[v1_key]
        combos[v1_idx] = v1
    else:
        if len(combos) >= budget:
            combos = combos[:-1]
        combos.append(v1)
        v1_idx = len(combos) - 1
    return combos, v1_idx, was_pruned, axes


def _param_str(p: dict) -> str:
    parts = []
    if "take_profit_mode" in p:
        parts.append(f"tp={p['take_profit_mode']}")
    parts.append(f"gate={p.get('gate', 'on')}")
    parts.append(f"be={_fmt_be(p)}")
    if "fvg_min_size_ticks" in p:
        parts.append(f"fvgmin={int(p['fvg_min_size_ticks'])}")
    if "session_filters" in p:
        parts.append("sess=" + ("+".join(p["session_filters"]) or "24h"))
    if "use_rsi_filter" in p or "use_vwap_filter" in p:
        f = [n for n, k in (("rsi", "use_rsi_filter"), ("vwap", "use_vwap_filter"))
             if p.get(k)]
        parts.append("filt=" + ("+".join(f) if f else "none"))
    return " ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Config builder (params overlay on the Phase-B builder)
# ─────────────────────────────────────────────────────────────────────────────

def _frontier_strategy_config(strat: dict, params: dict, inst: str):
    """Phase-B _real_strategy_config + the axes opt_worker never plumbs:
    take_profit_mode, session_filters override, rsi/vwap filter override."""
    cfg = _real_strategy_config(strat, params, inst)
    rt = strat.get("rule_tree") or {}
    cfg.take_profit_mode = str(
        params.get("take_profit_mode",
                   rt.get("take_profit_mode", "auto")) or "auto").lower()
    if "session_filters" in params:
        cfg.session_filters = list(params["session_filters"])
    if "use_rsi_filter" in params:
        cfg.use_rsi_filter = bool(params["use_rsi_filter"])
    if "use_vwap_filter" in params:
        cfg.use_vwap_filter = bool(params["use_vwap_filter"])
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Pool workers (module-level: spawn pickles by reference). This driver runs
# its OWN workers because opt_worker.run_combo drops take_profit_mode /
# session overrides and can't toggle the gate — but the DataHandler init and
# the metrics flattening are still the live optimizer's (init_worker /
# _metrics_dict). Never raises — errors ride back in the result dict.
# ─────────────────────────────────────────────────────────────────────────────

def _err_result(params: dict, e: Exception) -> dict:
    import traceback
    return {
        "params": params, "net_profit": 0, "profit_factor": 0, "win_rate": 0,
        "effective_win_rate": 0, "max_drawdown": 0, "total_trades": 0,
        "sharpe_ratio": 0, "tw_wins": 0, "tw_losses": 0, "tw_be": 0,
        "tw_total": 0, "tw_raw_wr": 0.0, "tw_eff_wr": 0.0,
        "_error": f"{type(e).__name__}: {e}",
        "_tb": traceback.format_exc()[-600:],
    }


def _train_combo_real(idx, params, strat, start_date, end_date):
    try:
        from app.engines.backtest_engine.ict_strategy import ICTStrategy
        inst = opt_worker._WORKER["instrument"]
        dh = opt_worker._WORKER["dh"].unfiltered_copy()
        cfg = _frontier_strategy_config(strat, params, inst)
        all_tfs = list(set([cfg.primary_timeframe, cfg.execution_timeframe]
                           + (cfg.higher_timeframes or [])))
        bt = _bt_config(inst, start_date, end_date, cfg.primary_timeframe,
                        all_tfs, params, strat, TRAIN_SLIPPAGE_TICKS)
        with _gate_disabled(params.get("gate") == "off"):
            runner = BacktestRunner(ICTStrategy(cfg, instrument=inst), dh, bt)
            m = runner.run()
        return idx, {"params": params, **opt_worker._metrics_dict(m),
                     **_three_way(runner.completed_trades)}
    except Exception as e:
        return idx, _err_result(params, e)


def _train_combo_synth(idx, params, name, inst, signal_every, stop_ticks,
                       start_date, end_date):
    try:
        dh = opt_worker._WORKER["dh"].unfiltered_copy()
        cfg = _synthetic_strategy_config(params, name, inst)
        strategy = SyntheticPulseStrategy(
            cfg, instrument=inst, signal_every=signal_every, stop_ticks=stop_ticks)
        bt = _bt_config(inst, start_date, end_date, "1m", ["1m", "15m"],
                        params, {"breakeven_at_r": 0.0, "breakeven_mode": "off"},
                        TRAIN_SLIPPAGE_TICKS)
        with _gate_disabled(params.get("gate") == "off"):
            runner = BacktestRunner(strategy, dh, bt)
            m = runner.run()
        return idx, {"params": params, **opt_worker._metrics_dict(m),
                     **_three_way(runner.completed_trades)}
    except Exception as e:
        return idx, _err_result(params, e)


# ─────────────────────────────────────────────────────────────────────────────
# OOS legs (main process — fresh strategy + cheap handler copy per run)
# ─────────────────────────────────────────────────────────────────────────────

def _eval_oos(job: dict, params: dict, dh: DataHandler, oos_start: datetime,
              oos_end: datetime, slippage_ticks: int) -> dict:
    if job["synthetic"]:
        cfg = _synthetic_strategy_config(params, job["name"], job["inst"])
        strategy = SyntheticPulseStrategy(
            cfg, instrument=job["inst"],
            signal_every=job["spec"]["signal_every"],
            stop_ticks=job["spec"]["stop_ticks"])
        primary_tf, all_tfs = "1m", ["1m", "15m"]
    else:
        from app.engines.backtest_engine.ict_strategy import ICTStrategy
        cfg = _frontier_strategy_config(job["strat"], params, job["inst"])
        primary_tf = cfg.primary_timeframe
        all_tfs = list(set([cfg.primary_timeframe, cfg.execution_timeframe]
                           + (cfg.higher_timeframes or [])))
        strategy = ICTStrategy(cfg, instrument=job["inst"])
    bt = _bt_config(job["inst"], oos_start, oos_end, primary_tf, all_tfs,
                    params, job["strat"], slippage_ticks)
    with _gate_disabled(params.get("gate") == "off"):
        runner = BacktestRunner(strategy, dh.unfiltered_copy(), bt)
        m = runner.run()
    return {**opt_worker._metrics_dict(m), **_three_way(runner.completed_trades)}


# ─────────────────────────────────────────────────────────────────────────────
# Incremental output
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
    if key.endswith("_trades") or key.endswith("_be"):
        return str(int(v))
    if key.endswith("_wr_pct") or key.endswith("_dd_pct") or key.endswith("_tpw"):
        return f"{v:.1f}"
    if key.endswith("_net_usd"):
        return f"{v:.2f}"
    return f"{v:.2f}"


MD_ORDER = [
    "train_trades", "train_pf", "train_eff_wr_pct",
    "oos2t_trades", "oos2t_tpw", "oos2t_raw_wr_pct", "oos2t_eff_wr_pct",
    "oos2t_be", "oos2t_pf", "oos2t_dd_pct", "oos2t_net_usd",
    "oos1t_trades", "oos1t_tpw", "oos1t_raw_wr_pct", "oos1t_eff_wr_pct",
    "oos1t_be", "oos1t_pf", "oos1t_dd_pct", "oos1t_net_usd",
]


class FrontierWriter:
    """Header first; N rows appended after EACH strategy so a killed run
    leaves a valid partial CSV/MD behind."""

    def __init__(self, out_dir: Path, meta: dict):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.csv_path = out_dir / CSV_NAME
        self.md_path = out_dir / MD_NAME
        with open(self.csv_path, "w", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FIELDNAMES).writeheader()
        lines = [
            "# Strategy V2 — win-rate frontier (selectivity + structure targets, out-of-sample)",
            "",
            (f"_Generated {meta['generated_at']} · window {meta['start']} → {meta['end']} · "
             f"walk-forward split {meta['split']} (train head / last {meta['oos_fraction']:.0%} OOS) · "
             f"data: {meta['data_source']} · workers {meta['workers']} · "
             f"grid ≤{meta['max_combos']} combos (V1 config always included) · gate mode: {meta['gate']}._"),
            "",
            ("**Design**: win rate via SELECTIVITY (fewer, better trades — no-trade days are "
             "by design) and STRUCTURE TARGETS, never via shrinking a fixed R target. There is "
             "NO risk_reward_ratio axis; every combo keeps the strategy's current rr (engine "
             "fallback target / range sanity floor only). Axes: take_profit_mode {auto, range} "
             "— the only two modes the engine supports ('auto' = next-swing → HTF-FVG → R:R "
             "fallback hierarchy; 'range' = far extreme of the 60-bar dealing range, RANGE-TP-V1); "
             "market activity gate {on, off} (no wired toggle exists — OFF legs monkeypatch "
             "evaluate_activity_gate to its documented ABSTAIN value (None), restoring legacy "
             "session-window behavior; ON legs use the engine's futures threshold 0.40 unless "
             "FUTURES_GATE_GO_THRESHOLD overrides); session_filters {current, primary-only} "
             "(primary = first listed; NOTE: when the gate renders a verdict the session window "
             "is bypassed, so this axis bites on gate-off legs/abstain bars); confluence "
             "strictness {current, rsi+vwap} (the only config-exposed veto filters — "
             "displacement and sweep checks are hard-coded on); fvg_min_size_ticks {4, 6}; "
             "breakeven {off, 0.5R}."),
            "",
            (f"**Three-way outcome accounting**: a trade is BREAKEVEN if the engine tagged it "
             f"(exit_reason='breakeven') OR |net_pnl| ≤ max({BE_COMM_MULT:g}×round-trip "
             f"commission, {BE_RISK_FRAC:g}×initial risk $) — risk = |entry−stop|/tick×tick_value"
             f"×contracts on the traded contract. Raw WR = (wins+BE)/all (BE = non-loss, matches "
             f"backtest is_winner and live /signals/stats win_rate). Effective WR = "
             f"wins/(wins+losses), BE excluded (matches effective_win_rate book-wide). "
             f"Tr/wk = trades ÷ (window days/7) — the selectivity cost."),
            "",
            (f"**Pipeline**: every combo trains on the head window (own pool worker — "
             f"opt_worker drops take_profit_mode/session/gate — 1-tick optimizer-parity "
             f"slippage, V2_FAST_BACKTEST=0); top {TOP_N_TRAIN} by TRAIN effective WR among "
             f"combos with train PF ≥ {meta['min_pf']:g} and ≥ {MIN_TRAIN_TRADES} train trades "
             f"advance (if none qualify the PF bar relaxes to {RELAXED_TRAIN_PF:g}, then drops "
             f"entirely, and such rows are flagged TRAIN_RELAXED; the trade floor is never "
             f"relaxed). Finalists + the V1 baseline each run OOS TWICE: 2t = 2 "
             f"ticks/side (today's entries) and 1t = 1 tick/side (near-perfect-fill future). "
             f"MAX_HONEST_WR flag = highest OOS(2t) effective WR with OOS(2t) PF ≥ "
             f"{meta['min_pf']:g} and ≥ {MIN_OOS_TRADES} OOS trades; LOW_SAMPLE flag when "
             f"OOS(2t) trades < {LOW_SAMPLE_OOS}. No MAX_HONEST_WR row for a strategy = no "
             f"combo met the bar. Rows append incrementally — a partial file means the run "
             f"was killed mid-book."),
            "",
            "| Strategy | Inst | Role | Flags | Params | trainTr | trainPF | trainEffWR% "
            "| 2t Tr | 2t Tr/wk | 2t RawWR% | 2t EffWR% | 2t BE | 2t PF | 2t DD% | 2t Net$ "
            "| 1t Tr | 1t Tr/wk | 1t RawWR% | 1t EffWR% | 1t BE | 1t PF | 1t DD% | 1t Net$ |",
            "|---|---|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
        ]
        self.md_path.write_text("\n".join(lines) + "\n")

    def append_rows(self, rows: list[dict]) -> None:
        with open(self.csv_path, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDNAMES)
            for row in rows:
                w.writerow({k: _fmt_cell(k, row.get(k)) for k in FIELDNAMES})
        with open(self.md_path, "a") as fh:
            for row in rows:
                cells = [row["strategy"], row["instrument"], row["role"],
                         row.get("flags") or "", row.get("params") or ""]
                cells += [_fmt_cell(k, row.get(k)) for k in MD_ORDER]
                fh.write("| " + " | ".join(cells) + " |\n")


# ─────────────────────────────────────────────────────────────────────────────
# Per-strategy pipeline
# ─────────────────────────────────────────────────────────────────────────────

def _oos_cols(label: str, m: Optional[dict], span_days: float) -> dict:
    if m is None:
        return {f"oos{label}_{k}": None for k in
                ("trades", "tpw", "raw_wr_pct", "eff_wr_pct", "be", "pf",
                 "dd_pct", "net_usd")}
    trades = int(m.get("total_trades") or 0)
    weeks = max(span_days / 7.0, 1e-9)
    return {
        f"oos{label}_trades": trades,
        f"oos{label}_tpw": trades / weeks,
        f"oos{label}_raw_wr_pct": float(m.get("tw_raw_wr") or 0.0) * 100.0,
        f"oos{label}_eff_wr_pct": float(m.get("tw_eff_wr") or 0.0) * 100.0,
        f"oos{label}_be": int(m.get("tw_be") or 0),
        f"oos{label}_pf": _pf_of(m),
        f"oos{label}_dd_pct": float(m.get("max_drawdown") or 0.0),
        f"oos{label}_net_usd": float(m.get("net_profit") or 0.0),
    }


def process_job(job: dict, args: argparse.Namespace, split: datetime,
                train_end: datetime, writer: FrontierWriter) -> None:
    name, inst = job["name"], job["inst"]
    combos, v1_idx, was_pruned, axes = build_grid(job, args)
    n = len(combos)
    live_axes = [ax for ax, key in (("tp", "tpm"), ("gate", "gate"),
                                    ("sess", "sess"), ("filt", "filt"),
                                    ("fvg", "fvg"))
                 if len(axes[key]) > 1] + ["be"]
    logger.info(
        f"[frontier] {name} ({inst}): {n} combos (live axes: {'/'.join(live_axes)}"
        + (", pruned" if was_pruned else "")
        + f"; V1 combo #{v1_idx}: {_param_str(combos[v1_idx])}) | "
        f"train [{args.start} → {split:%Y-%m-%d %H:%M}) / OOS [{split:%Y-%m-%d %H:%M} → {args.end}]"
    )

    # ── Train phase: every combo on the head window in the pool ─────────────
    records = job["df"].to_dict("list")
    train: list[Optional[dict]] = [None] * n
    workers = max(1, min(args.workers, n))
    ctx = mp.get_context("spawn")
    with cf.ProcessPoolExecutor(
            max_workers=workers, mp_context=ctx,
            initializer=_pool_init,
            initargs=(records, inst, job["base_tf"], list(job["tfs"]))) as pool:
        futs = {}
        for i, p in enumerate(combos):
            if job["synthetic"]:
                fut = pool.submit(_train_combo_synth, i, p, name, inst,
                                  job["spec"]["signal_every"],
                                  job["spec"]["stop_ticks"],
                                  args.start_dt, train_end)
            else:
                fut = pool.submit(_train_combo_real, i, p, job["strat"],
                                  args.start_dt, train_end)
            futs[fut] = i
        done = failed = 0
        for fut in cf.as_completed(futs):
            idx, res = fut.result()
            train[idx] = res
            done += 1
            if res.get("_error"):
                failed += 1
                logger.warning(f"[frontier] {name}: train {done}/{n} combo#{idx} "
                               f"FAILED — {res['_error']}")
            else:
                logger.info(
                    f"[frontier] {name}: train {done}/{n} combo#{idx} "
                    f"[{_param_str(combos[idx])}] effWR={res['tw_eff_wr']:.0%} "
                    f"rawWR={res['tw_raw_wr']:.0%} be={res['tw_be']} "
                    f"pf={_pf_of(res):.2f} trades={int(res.get('total_trades') or 0)} "
                    f"net=${float(res.get('net_profit') or 0.0):,.0f}")
    if failed:
        logger.warning(f"[frontier] {name}: {failed}/{n} train combos failed")

    # ── Advance top-N by TRAIN effective WR (PF & trade floors; relaxable) ──
    def _eligible(min_pf: float) -> list[tuple]:
        out = []
        for i, res in enumerate(train):
            if not res or res.get("_error"):
                continue
            if int(res.get("total_trades") or 0) < MIN_TRAIN_TRADES:
                continue
            if _pf_of(res) < min_pf:
                continue
            out.append((float(res["tw_eff_wr"]), _pf_of(res),
                        float(res.get("net_profit") or 0.0), i))
        out.sort(reverse=True)
        return out

    relaxed = False
    scored = _eligible(args.min_pf)
    if not scored:
        for fallback_pf in (RELAXED_TRAIN_PF, 0.0):
            scored = _eligible(fallback_pf)
            if scored:
                relaxed = True
                logger.warning(
                    f"[frontier] {name}: no combo met train PF ≥ {args.min_pf:g} "
                    f"— bar relaxed to {fallback_pf:g} "
                    f"(rows flagged TRAIN_RELAXED)")
                break
    top = [i for _, _, _, i in scored[:TOP_N_TRAIN]]
    if top:
        logger.info(f"[frontier] {name}: advancing {len(top)} → " + "; ".join(
            f"#{i} [{_param_str(combos[i])}] effWR={e:.0%} pf={pf:.2f}"
            for e, pf, _, i in scored[:TOP_N_TRAIN]))
    else:
        logger.warning(f"[frontier] {name}: NO combo produced ≥ "
                       f"{MIN_TRAIN_TRADES} train trades (floor never relaxed) "
                       f"— OOS runs V1 baseline only")

    # ── OOS showdown: finalists + V1, at BOTH slippage scenarios ────────────
    showdown: list[int] = []
    for i in [v1_idx] + top:
        if i not in showdown:
            showdown.append(i)
    dh = DataHandler(instrument=inst, base_timeframe=job["base_tf"])
    dh.load_from_dataframe(job["df"])
    dh.build_timeframes(list(job["tfs"]))
    span_days = (args.end_dt - split).total_seconds() / 86400.0
    oos: dict[int, dict[str, dict]] = {}
    for i in showdown:
        oos[i] = {}
        for label, ticks in OOS_SLIPPAGE_SCENARIOS:
            try:
                m = _eval_oos(job, combos[i], dh, split, args.end_dt, ticks)
            except Exception as e:
                logger.error(f"[frontier] {name}: OOS combo#{i} @{label} failed: "
                             f"{type(e).__name__}: {e}")
                m = None
            oos[i][label] = m
            if m:
                logger.info(
                    f"[frontier] {name}: OOS[{label}] combo#{i}"
                    f"{' (V1)' if i == v1_idx else ''} [{_param_str(combos[i])}] "
                    f"effWR={m['tw_eff_wr']:.0%} rawWR={m['tw_raw_wr']:.0%} "
                    f"be={m['tw_be']} pf={_pf_of(m):.2f} "
                    f"trades={int(m.get('total_trades') or 0)} "
                    f"net=${float(m.get('net_profit') or 0.0):,.0f}")

    # ── MAX-HONEST-WR pick on the selection scenario ─────────────────────────
    def _sel(i) -> Optional[dict]:
        return oos[i].get(SELECT_SCENARIO)

    pickable = [i for i in showdown if _sel(i)
                and int(_sel(i).get("total_trades") or 0) >= MIN_OOS_TRADES
                and _pf_of(_sel(i)) >= args.min_pf]
    pick = max(pickable, key=lambda i: (float(_sel(i)["tw_eff_wr"]),
                                        _pf_of(_sel(i)),
                                        float(_sel(i).get("net_profit") or 0.0))) \
        if pickable else None
    if pick is None:
        logger.warning(f"[frontier] {name}: NO combo met the MAX-HONEST-WR bar "
                       f"(OOS PF ≥ {args.min_pf:g}, ≥ {MIN_OOS_TRADES} trades)")

    # ── Rows: V1 baseline first, then finalists by OOS(2t) effective WR ─────
    rest = [i for i in showdown if i != v1_idx]
    rest.sort(key=lambda i: (float(_sel(i)["tw_eff_wr"]) if _sel(i) else -1.0),
              reverse=True)
    rows = []
    for i in [v1_idx] + rest:
        tr = train[i] if train[i] and not train[i].get("_error") else None
        s = _sel(i)
        flags = []
        if i == pick:
            flags.append("MAX_HONEST_WR")
        if s and int(s.get("total_trades") or 0) < LOW_SAMPLE_OOS:
            flags.append("LOW_SAMPLE")
        if relaxed and i != v1_idx:
            flags.append("TRAIN_RELAXED")
        row = {
            "strategy": name, "instrument": inst,
            "role": "V1_BASELINE" if i == v1_idx else "FRONTIER",
            "flags": ",".join(flags),
            "gate": combos[i].get("gate", "on"),
            "tp_mode": combos[i].get("take_profit_mode", ""),
            "params": _param_str(combos[i]),
            "train_trades": (int(tr.get("total_trades") or 0) if tr else None),
            "train_pf": (_pf_of(tr) if tr else None),
            "train_eff_wr_pct": (float(tr["tw_eff_wr"]) * 100.0 if tr else None),
            "params_json": json.dumps(combos[i], sort_keys=True),
        }
        for label, _ticks in OOS_SLIPPAGE_SCENARIOS:
            row.update(_oos_cols(label, oos[i].get(label), span_days))
        rows.append(row)
    writer.append_rows(rows)
    logger.info(
        f"[frontier] {name}: {len(rows)} rows appended | pick="
        + (f"#{pick} [{_param_str(combos[pick])}] "
           f"effWR={_sel(pick)['tw_eff_wr']:.0%} pf={_pf_of(_sel(pick)):.2f}"
           if pick is not None else "none"))


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Win-rate frontier: selectivity + structure-target grid per "
                    "active futures strategy, three-way outcome accounting, "
                    "dual-slippage OOS showdown (see module docstring).")
    ap.add_argument("--strategy-name", action="append", dest="strategy_names",
                    metavar="NAME", help="strategy name to run (repeatable)")
    ap.add_argument("--all", action="store_true",
                    help="run every active futures strategy "
                         "(implied by --synthetic when no names are given)")
    ap.add_argument("--start", default="2026-04-01", help="window start YYYY-MM-DD")
    ap.add_argument("--end", default="2026-05-01", help="window end YYYY-MM-DD")
    ap.add_argument("--oos-fraction", type=float, default=0.3,
                    help="fraction of the window held out-of-sample at the END "
                         "(default 0.3)")
    ap.add_argument("--max-combos", type=int, default=48,
                    help="cap on grid combos per strategy incl. the V1 combo "
                         "(default 48)")
    ap.add_argument("--workers", type=int, default=3,
                    help="ProcessPool cap for the train phase (default 3)")
    ap.add_argument("--min-pf", type=float, default=2.0,
                    help="profit-factor floor for train advancement and the "
                         "MAX-HONEST-WR pick (default 2.0)")
    ap.add_argument("--gate", choices=("on", "off", "both"), default="both",
                    help="market-activity-gate axis: both (grid axis, default), "
                         "on, or off for every combo")
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
        ap.error("--oos-fraction must be in (0, 0.9)")
    if args.max_combos < 2:
        ap.error("--max-combos must be >= 2")
    if args.workers < 1:
        ap.error("--workers must be >= 1")
    if args.min_pf <= 0:
        ap.error("--min-pf must be > 0")
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if not args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="INFO")

    logger.info(f"[frontier] V2_FAST_BACKTEST={os.environ.get('V2_FAST_BACKTEST', '<unset>')} "
                f"(run with 0 — this driver is validated on the stable engine path only)")

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
        logger.error("[frontier] nothing to run")
        return 1

    meta = {
        "generated_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "start": args.start, "end": args.end,
        "split": split.strftime("%Y-%m-%d %H:%M"),
        "oos_fraction": args.oos_fraction,
        "max_combos": args.max_combos, "workers": args.workers,
        "min_pf": args.min_pf, "gate": args.gate,
        "data_source": data_source,
    }
    writer = FrontierWriter(Path(args.output_dir), meta)
    logger.info(f"[frontier] {len(jobs)} strategies queued; incremental output → "
                f"{writer.csv_path} / {writer.md_path}")

    ok = 0
    for pos, job in enumerate(jobs, start=1):
        logger.info(f"[frontier] ── strategy {pos}/{len(jobs)}: {job['name']} ──")
        try:
            process_job(job, args, split, train_end, writer)
            ok += 1
        except Exception as exc:
            logger.error(f"[frontier] {job['name']!r} failed: "
                         f"{type(exc).__name__}: {exc}")
            continue

    logger.info(f"[frontier] done: {ok}/{len(jobs)} strategies written to "
                f"{writer.csv_path} and {writer.md_path}")
    print(writer.md_path.read_text())
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
