"""
Optimization Engine — grid search + Celery task distribution.
Tests all combinations of strategy parameters and ranks results by a chosen metric.
"""
from dataclasses import replace as _dc_replace
from itertools import product
from typing import Any
from loguru import logger

from app.engines.strategy_engine.base_strategy import StrategyConfig
from app.engines.backtest_engine.backtest_runner import BacktestRunner, BacktestConfig
from app.engines.backtest_engine.data_handler import DataHandler
from app.engines.backtest_engine.metrics import BacktestMetricsResult


def build_parameter_combinations(grid: dict[str, list]) -> list[dict]:
    """
    Build all combinations from a parameter grid.
    grid = {"risk_reward_ratio": [1.5, 2.0, 2.5], "stop_loss_ticks": [8, 12]}
    Returns: [{"risk_reward_ratio": 1.5, "stop_loss_ticks": 8}, ...]
    """
    keys   = list(grid.keys())
    values = list(grid.values())
    combos = []
    for combo in product(*values):
        combos.append(dict(zip(keys, combo)))
    return combos


def run_optimization(
    base_config: StrategyConfig,
    parameter_grid: dict[str, list],
    data_handler: DataHandler,
    backtest_config: BacktestConfig,
    strategy_class,
    optimization_metric: str = "profit_factor",
    top_n: int = 10,
    oos_fraction: float = 0.0,
) -> list[dict]:
    """
    Run a full grid search optimization.
    Returns top_n results sorted by optimization_metric, descending.

    Each result is:
    {
        "rank": int,
        "parameters": dict,
        "metrics": BacktestMetricsResult,
    }

    oos_fraction=0.0 (default) is the exact original in-sample behavior.
    oos_fraction>0 enables walk-forward: the date window is time-split so the
    LAST oos_fraction of it is a held-out out-of-sample segment. Each combo is
    backtested on the train head and RANKED by optimization_metric computed on
    the holdout (ranking in-sample is what overfits). In that mode each ranked
    result additionally carries "train_metrics" and "oos_metrics", and
    "metrics" is the OOS set — the one the ranking used.
    """
    combinations = build_parameter_combinations(parameter_grid)
    logger.info(f"Optimization: testing {len(combinations)} parameter combinations")

    oos_fraction = float(oos_fraction or 0.0)
    walk_forward = oos_fraction > 0.0
    if walk_forward:
        # Same split rule as the live ProcessPool path (opt_worker), so both
        # optimizer implementations hold out an identical OOS window. The
        # train end backs off TRAIN_END_EPSILON because filter_date_range is
        # inclusive on BOTH ends — a bar landing exactly on the split belongs
        # ONLY to the OOS window (no holdout bar may leak into training).
        from app.engines.optimization_engine.opt_worker import (
            TRAIN_END_EPSILON, split_walkforward,
        )
        split = split_walkforward(backtest_config.start_date, backtest_config.end_date, oos_fraction)
        train_bt = _dc_replace(backtest_config, end_date=split - TRAIN_END_EPSILON)
        oos_bt = _dc_replace(backtest_config, start_date=split)
        # One handler per window: BacktestRunner.run() filters its handler
        # destructively (no-op only for an IDENTICAL range), so train and OOS
        # must never share one — the second window would intersect the first
        # window's trim down to at most the split-boundary bar. Copies are
        # O(1) (frames are shared, never mutated in place); within a window
        # every combo filters the same range, so reuse across combos is the
        # same idempotent no-op V1 relies on.
        train_dh = data_handler.unfiltered_copy()
        oos_dh = data_handler.unfiltered_copy()
        logger.info(
            f"Optimization walk-forward (oos_fraction={oos_fraction}): "
            f"train [{backtest_config.start_date} -> {split}) / "
            f"oos [{split} -> {backtest_config.end_date}]"
        )

    results = []

    for i, params in enumerate(combinations):
        # Clone base config with this combination's params
        config_dict = {
            "name": base_config.name,
            "instruments": base_config.instruments,
            "primary_timeframe": params.get("primary_timeframe", base_config.primary_timeframe),
            "execution_timeframe": params.get("execution_timeframe", base_config.execution_timeframe),
            "higher_timeframes": base_config.higher_timeframes,
            "risk_reward_ratio": params.get("risk_reward_ratio", base_config.risk_reward_ratio),
            "stop_loss_type": base_config.stop_loss_type,
            "stop_loss_ticks": params.get("stop_loss_ticks", base_config.stop_loss_ticks),
            "max_contracts": base_config.max_contracts,
            "session_filters": base_config.session_filters,
            "fvg_min_size_ticks": params.get("fvg_min_size_ticks", base_config.fvg_min_size_ticks),
            "fvg_max_size_ticks": params.get("fvg_max_size_ticks", base_config.fvg_max_size_ticks),
            "max_daily_loss": base_config.max_daily_loss,
            "max_trades_per_day": base_config.max_trades_per_day,
        }

        try:
            config = StrategyConfig(**config_dict)
            if not walk_forward:
                # ── V1 path: single in-sample run (exact parity) ──
                strategy = strategy_class(config)
                runner = BacktestRunner(strategy, data_handler, backtest_config)
                metrics = runner.run()

                score = getattr(metrics, optimization_metric, 0.0) or 0.0
                results.append({
                    "parameters": params,
                    "metrics": metrics,
                    "score": score,
                })
            else:
                # ── Walk-forward: fresh strategy instance per window —
                # strategies carry per-run state, never share one. Each
                # window also gets ITS OWN handler (see clones above). ──
                train_metrics = BacktestRunner(strategy_class(config), train_dh, train_bt).run()
                oos_metrics = BacktestRunner(strategy_class(config), oos_dh, oos_bt).run()

                # Rank by the SAME metric, but computed on the holdout.
                score = getattr(oos_metrics, optimization_metric, 0.0) or 0.0
                results.append({
                    "parameters": params,
                    "metrics": oos_metrics,
                    "train_metrics": train_metrics,
                    "oos_metrics": oos_metrics,
                    "score": score,
                })

            if (i + 1) % 10 == 0:
                logger.info(f"  {i + 1}/{len(combinations)} combinations tested...")

        except Exception as e:
            logger.warning(f"Combination {i} failed: {params} — {e}")
            continue

    # Sort descending by score
    results.sort(key=lambda x: x["score"], reverse=True)

    # Assign ranks
    ranked = []
    for rank, r in enumerate(results[:top_n], start=1):
        entry = {
            "rank": rank,
            "parameters": r["parameters"],
            "metrics": r["metrics"],
        }
        if walk_forward:
            # Keep both metric sets visible so a consumer can compare
            # in-sample vs out-of-sample degradation per combo.
            entry["train_metrics"] = r["train_metrics"]
            entry["oos_metrics"] = r["oos_metrics"]
        ranked.append(entry)

    logger.info(f"Optimization complete. Top result: {ranked[0]['parameters'] if ranked else 'none'}")
    return ranked
