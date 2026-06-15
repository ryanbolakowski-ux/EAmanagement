"""Process-pool worker for parallel backtest optimization.

Lives in its OWN lightweight module so `spawn`-ed worker processes re-import only
this + the backtest engine (pandas/numpy/strategy), NOT the FastAPI app/router.
Each worker builds the shared DataHandler ONCE via `init_worker` (initializer)
and then runs combos on its own CPU core — real parallelism, which threads can't
provide for CPU-bound Python because of the GIL. No DB access: pure compute.
"""
from __future__ import annotations

_WORKER: dict = {}  # per-process state: prebuilt DataHandler + instrument


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


def run_combo(idx, params, strat, start_date, end_date):
    """Run a single parameter combination's backtest. Returns (idx, result dict).
    Never raises — failures are captured in the result so one bad combo can't
    sink the whole optimization."""
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
        strategy = ICTStrategy(config, instrument=inst)
        all_tfs = list(set([config.primary_timeframe, config.execution_timeframe] + (config.higher_timeframes or [])))
        _be = params.get("breakeven_at_r", strat.get("breakeven_at_r"))
        _be_mode = params.get("breakeven_mode", strat.get("breakeven_mode")) or "off"
        bt = BacktestConfig(
            instrument=inst, start_date=start_date, end_date=end_date,
            primary_timeframe=config.primary_timeframe, all_timeframes=all_tfs,
            initial_capital=100000, commission_per_side=2.50, slippage_ticks=1,
            breakeven_at_r=float(_be if _be is not None else 0.0),
            breakeven_mode=str(_be_mode),
        )
        m = BacktestRunner(strategy, dh, bt).run()
        return idx, {
            "params": params,
            "net_profit": float(m.net_profit), "profit_factor": float(m.profit_factor),
            "win_rate": float(m.win_rate),
            "effective_win_rate": float(getattr(m, "effective_win_rate", m.win_rate)),
            "max_drawdown": float(m.max_drawdown_pct), "total_trades": int(m.total_trades),
            "sharpe_ratio": float(m.sharpe_ratio or 0),
        }
    except Exception as e:
        import traceback
        return idx, {
            "params": params, "net_profit": 0, "profit_factor": 0, "win_rate": 0,
            "effective_win_rate": 0, "max_drawdown": 0, "total_trades": 0, "sharpe_ratio": 0,
            "_error": f"{type(e).__name__}: {e}", "_tb": traceback.format_exc()[-600:],
        }
