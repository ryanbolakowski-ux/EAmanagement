"""Profile one real ES strategy backtest over ~2 weeks.

Mirrors app/api/routes/backtests.py::_run_backtest_task construction exactly:
fetch_futures_data -> DataHandler -> ICTStrategy(StrategyConfig) -> BacktestRunner.
Strategy: "FVG Inversion Tap" (73ffc80b) — real DB row values hardcoded below.
"""
import asyncio, cProfile, pstats, io, time, os, sys
from datetime import datetime

from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
from app.engines.backtest_engine.data_handler import DataHandler
from app.engines.backtest_engine.backtest_runner import BacktestRunner, BacktestConfig
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig


async def main():
    start = datetime(2026, 6, 1)
    end = datetime(2026, 6, 14)
    df = await fetch_futures_data(
        instrument="ES", start_date=start, end_date=end,
        interval="1m", use_polygon=False,
    )
    print(f"DATA rows={len(df)} range={df.index[0]}..{df.index[-1]}", flush=True)

    # Values from strategies row 73ffc80b-5e7d-4b94-8baa-e448ef042139
    config = StrategyConfig(
        name="FVG Inversion Tap",
        instruments=["ES", "NQ"],
        primary_timeframe="15m",
        execution_timeframe="1m",
        higher_timeframes=["1H", "4H"],
        risk_reward_ratio=3.0,
        stop_loss_type="structure",
        stop_loss_ticks=None,
        max_contracts=10,
        session_filters=["NY_AM", "LONDON"],
        fvg_min_size_ticks=4,
        fvg_max_size_ticks=None,
        max_daily_loss=None,
        max_trades_per_day=5,
        use_rsi_filter=False,
        use_vwap_filter=False,
    )
    config.rule_tree = {}
    config.take_profit_mode = "auto"

    strategy = ICTStrategy(config, instrument="ES")
    data_handler = DataHandler(instrument="ES", base_timeframe="1m")
    data_handler.load_from_dataframe(df.reset_index())

    all_tfs = list(set([config.primary_timeframe, config.execution_timeframe] + config.higher_timeframes))
    bt_config = BacktestConfig(
        instrument="ES", start_date=start, end_date=end,
        primary_timeframe=config.primary_timeframe, all_timeframes=all_tfs,
        initial_capital=100_000.0, commission_per_side=2.25, slippage_ticks=1,
        risk_per_trade_pct=1.0, trailing_drawdown=0.0, daily_loss_limit=0.0,
        breakeven_at_r=0.5, breakeven_mode="r",
    )
    runner = BacktestRunner(strategy, data_handler, bt_config)

    pr = cProfile.Profile()
    t0 = time.perf_counter()
    pr.enable()
    metrics = runner.run()
    pr.disable()
    wall = time.perf_counter() - t0
    print(f"WALL={wall:.2f}s trades={metrics.total_trades} net={metrics.net_profit:.2f} wr={metrics.win_rate:.3f}", flush=True)

    for sort in ("cumulative", "tottime"):
        s = io.StringIO()
        ps = pstats.Stats(pr, stream=s).sort_stats(sort)
        ps.print_stats(30)
        print(f"===== SORT {sort} =====")
        print(s.getvalue())

asyncio.run(main())
