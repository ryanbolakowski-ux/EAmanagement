"""Process-pool worker for parallel backtest optimization.

Lives in its OWN lightweight module so `spawn`-ed worker processes re-import only
this + the backtest engine (pandas/numpy/strategy), NOT the FastAPI app/router.
Each worker builds the shared DataHandler ONCE via `init_worker` (initializer)
and then runs combos on its own CPU core — real parallelism, which threads can't
provide for CPU-bound Python because of the GIL. No DB access: pure compute.

Walk-forward (oos_fraction > 0): the window is time-split so the LAST
oos_fraction of it is a held-out out-of-sample segment. Each combo is
backtested on the train segment AND (with a fresh strategy instance) on the
holdout; the result's TOP-LEVEL metric keys carry the OOS numbers so the
existing rank-by-metric code upstream automatically ranks on out-of-sample
performance — the whole point of walk-forward is that in-sample rank order
is what overfits. Both metric sets are kept (train_* / oos_* prefixes).
Each window runs on a cheap `unfiltered_copy()` of the shared DataHandler —
its filter_date_range trims destructively (no-op only for an identical
range), so filtering the shared handler to two different windows would leave
every later run with at most the split-boundary bar.
oos_fraction=0.0 (default) is byte-for-byte the original single-run behavior.
"""
from __future__ import annotations

from datetime import timedelta

_WORKER: dict = {}  # per-process state: prebuilt DataHandler + instrument

# DataHandler.filter_date_range is inclusive on BOTH ends, but the spec's
# train window is [start, split) — a bar landing exactly on the split must
# belong ONLY to the OOS window, never both (that would leak one holdout bar
# into training). One microsecond is smaller than any bar interval, so
# backing the train end off by this drops exactly that boundary bar.
TRAIN_END_EPSILON = timedelta(microseconds=1)

# Metric keys replicated into the train_/oos_ prefixed sets in walk-forward mode.
_METRIC_KEYS = ("net_profit", "profit_factor", "win_rate", "effective_win_rate",
                "max_drawdown", "total_trades", "sharpe_ratio")


def split_walkforward(start_date, end_date, oos_fraction):
    """Return the split datetime such that [start_date, split) is the train
    window and [split, end_date] is the out-of-sample holdout containing the
    LAST `oos_fraction` of the total span. Pure so tests can pin boundaries."""
    span = end_date - start_date
    return start_date + span * (1.0 - float(oos_fraction))


def _metrics_dict(m) -> dict:
    """Flatten a BacktestMetricsResult into the plain result-dict shape."""
    return {
        "net_profit": float(m.net_profit), "profit_factor": float(m.profit_factor),
        "win_rate": float(m.win_rate),
        "effective_win_rate": float(getattr(m, "effective_win_rate", m.win_rate)),
        "max_drawdown": float(m.max_drawdown_pct), "total_trades": int(m.total_trades),
        "sharpe_ratio": float(m.sharpe_ratio or 0),
    }


def init_worker(df_records, instrument, base_tf, tfs):
    """Runs ONCE per worker process. Rebuilds the shared DataHandler so every
    combo this process runs reuses it (resample once, not per-combo)."""
    import pandas as pd
    from app.engines.backtest_engine.data_handler import DataHandler
    df = pd.DataFrame(df_records)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    dh = DataHandler(instrument=instrument, base_timeframe=base_tf)
    dh.load_from_dataframe(df)
    dh.build_timeframes(list(tfs))
    _WORKER["dh"] = dh
    _WORKER["instrument"] = instrument


def run_combo(idx, params, strat, start_date, end_date, oos_fraction=0.0):
    """Run a single parameter combination's backtest. Returns (idx, result dict).
    Never raises — failures are captured in the result so one bad combo can't
    sink the whole optimization.

    oos_fraction=0.0 (default) preserves the original behavior exactly.
    oos_fraction>0 runs walk-forward: train on the first (1-oos_fraction) of
    the window, evaluate on the held-out remainder; top-level metrics = OOS."""
    from app.engines.strategy_engine.base_strategy import StrategyConfig
    from app.engines.backtest_engine.ict_strategy import ICTStrategy
    from app.engines.backtest_engine.backtest_runner import BacktestRunner, BacktestConfig
    inst = _WORKER["instrument"]; dh = _WORKER["dh"]
    try:
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
        all_tfs = list(set([config.primary_timeframe, config.execution_timeframe] + (config.higher_timeframes or [])))
        _be = params.get("breakeven_at_r", strat.get("breakeven_at_r"))
        _be_mode = params.get("breakeven_mode", strat.get("breakeven_mode")) or "off"

        def _run_window(w_start, w_end, w_dh):
            """One backtest over [w_start, w_end] with a FRESH strategy —
            ICTStrategy carries per-run state (buffers, daily counters), so
            the train and OOS passes must never share an instance."""
            strategy = ICTStrategy(config, instrument=inst)
            bt = BacktestConfig(
                instrument=inst, start_date=w_start, end_date=w_end,
                primary_timeframe=config.primary_timeframe, all_timeframes=all_tfs,
                initial_capital=100000, commission_per_side=2.50, slippage_ticks=1,
                breakeven_at_r=float(_be if _be is not None else 0.0),
                breakeven_mode=str(_be_mode),
            )
            return BacktestRunner(strategy, w_dh, bt).run()

        oos_fraction = float(oos_fraction or 0.0)
        if oos_fraction <= 0.0:
            # ── V1 path: single run over the full window (exact parity).
            # Sharing the per-process dh directly is safe ONLY here: every
            # combo filters it to the same range (idempotent no-op). ──
            m = _run_window(start_date, end_date, dh)
            return idx, {"params": params, **_metrics_dict(m)}

        # ── Walk-forward: train on the head, hold out the tail ──────────
        # Each window gets its own cheap handler copy: BacktestRunner.run()
        # filters its handler destructively, so running train then OOS on
        # the shared dh would intersect the trims — the OOS pass (and BOTH
        # windows of every later combo in this worker process) would see at
        # most the single split-boundary bar. The copies keep the shared dh
        # pristine; TRAIN_END_EPSILON keeps the split bar out of training.
        split = split_walkforward(start_date, end_date, oos_fraction)
        train_m = _metrics_dict(
            _run_window(start_date, split - TRAIN_END_EPSILON, dh.unfiltered_copy()))
        oos_m = _metrics_dict(_run_window(split, end_date, dh.unfiltered_copy()))
        result = {"params": params, "wf_split": split.isoformat(),
                  "oos_fraction": oos_fraction}
        # Top-level keys = OOS metrics so the existing rank/sort upstream
        # ranks by the SAME metric computed on the holdout.
        result.update(oos_m)
        for k in _METRIC_KEYS:
            result[f"train_{k}"] = train_m[k]
            result[f"oos_{k}"] = oos_m[k]
        return idx, result
    except Exception as e:
        import traceback
        return idx, {
            "params": params, "net_profit": 0, "profit_factor": 0, "win_rate": 0,
            "effective_win_rate": 0, "max_drawdown": 0, "total_trades": 0, "sharpe_ratio": 0,
            "_error": f"{type(e).__name__}: {e}", "_tb": traceback.format_exc()[-600:],
        }
