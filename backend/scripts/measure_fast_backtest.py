"""FAST-BT-V1 STEP-4 measurement: same backtest, flag OFF vs ON, wall-clock
median of 2 runs each. Engine-only timing (BacktestRunner.run), data loaded
once up front — mirrors the [PROFILE] engine_run metric the route logs.

Usage (ephemeral container, --cpus=2):
    python -m scripts.measure_fast_backtest
"""
import asyncio
import os
import statistics
import time
from datetime import datetime

from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
from app.engines.backtest_engine.data_handler import DataHandler
from app.engines.backtest_engine.backtest_runner import BacktestRunner, BacktestConfig
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig

WINDOWS = [
    ("2-week", datetime(2026, 6, 1), datetime(2026, 6, 14)),
    ("6-week", datetime(2026, 5, 18), datetime(2026, 6, 29)),
]


def _one_run(df, start, end, fast: bool):
    config = StrategyConfig(
        name="FVG Inversion Tap", instruments=["ES", "NQ"],
        primary_timeframe="15m", execution_timeframe="1m",
        higher_timeframes=["1H", "4H"], risk_reward_ratio=3.0,
        stop_loss_type="structure", stop_loss_ticks=None, max_contracts=10,
        session_filters=["NY_AM", "LONDON"], fvg_min_size_ticks=4,
        fvg_max_size_ticks=None, max_daily_loss=None, max_trades_per_day=5,
        use_rsi_filter=False, use_vwap_filter=False,
    )
    config.rule_tree = {}
    config.take_profit_mode = "auto"
    strategy = ICTStrategy(config, instrument="ES")
    handler = DataHandler(instrument="ES", base_timeframe="1m")
    handler.load_from_dataframe(df)
    bt = BacktestConfig(
        instrument="ES", start_date=start, end_date=end,
        primary_timeframe="15m", all_timeframes=list(set(["15m", "1m", "1H", "4H"])),
        initial_capital=100_000.0, commission_per_side=2.25, slippage_ticks=1,
        risk_per_trade_pct=1.0, trailing_drawdown=0.0, daily_loss_limit=0.0,
        breakeven_at_r=0.5, breakeven_mode="r",
    )
    os.environ["V2_FAST_BACKTEST"] = "1" if fast else "0"
    runner = BacktestRunner(strategy, handler, bt)
    t0 = time.perf_counter()
    metrics = runner.run()
    wall = time.perf_counter() - t0
    return wall, metrics


async def main():
    for win_label, start, end in WINDOWS:
        df = await fetch_futures_data(instrument="ES", start_date=start, end_date=end,
                                      interval="1m", use_polygon=False)
        df = df.reset_index()
        print(f"[{win_label}] DATA rows={len(df)}", flush=True)
        results = {}
        for label, fast in (("old", False), ("fast", True), ("old", False), ("fast", True)):
            wall, m = _one_run(df.copy(), start, end, fast)
            results.setdefault(label, []).append(wall)
            print(f"[{win_label}] {label:4s} wall={wall:7.2f}s trades={m.total_trades} "
                  f"net={m.net_profit:.2f} wr={m.win_rate:.4f}", flush=True)
        old_med = statistics.median(results["old"])
        fast_med = statistics.median(results["fast"])
        print(f"[{win_label}] MEDIAN old={old_med:.2f}s fast={fast_med:.2f}s "
              f"speedup={old_med / fast_med:.2f}x", flush=True)

asyncio.run(main())
