"""
Optimization Engine — grid search + Celery task distribution.
Tests all combinations of strategy parameters and ranks results by a chosen metric.
"""
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
    """
    combinations = build_parameter_combinations(parameter_grid)
    logger.info(f"Optimization: testing {len(combinations)} parameter combinations")

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
            strategy = strategy_class(config)
            runner = BacktestRunner(strategy, data_handler, backtest_config)
            metrics = runner.run()

            score = getattr(metrics, optimization_metric, 0.0) or 0.0
            results.append({
                "parameters": params,
                "metrics": metrics,
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
        ranked.append({
            "rank": rank,
            "parameters": r["parameters"],
            "metrics": r["metrics"],
        })

    logger.info(f"Optimization complete. Top result: {ranked[0]['parameters'] if ranked else 'none'}")
    return ranked
