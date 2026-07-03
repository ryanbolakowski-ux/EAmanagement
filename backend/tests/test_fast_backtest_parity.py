"""FAST-BT-V1 parity gate.

V2_FAST_BACKTEST=1 (the vectorized fast path) must produce a trade list and
metrics IDENTICAL to V2_FAST_BACKTEST=0 (the original per-bar pandas path):
same ordered trades — entry/exit times, prices, stops, targets, contracts,
pnl — and same metrics, over a >= 6-week window, for two real strategies.

Data: real 1m bars via the route's fetch_futures_data/local candle cache when
DATABASE_URL is reachable; otherwise the deterministic synthetic generator
from scripts/strategy_v2_harness.py (seeded — byte-identical across runs).

Construction mirrors app/api/routes/backtests.py::_run_backtest_task.
"""
import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # /app for scripts.*

from app.engines.backtest_engine.backtest_runner import BacktestRunner, BacktestConfig
from app.engines.backtest_engine.data_handler import DataHandler
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig

# >= 6 weeks (42 days)
START = datetime(2026, 5, 18)
END = datetime(2026, 6, 29)

# Real DB rows (strategies table), same fields the route copies into
# StrategyConfig / BacktestConfig.
STRATEGIES = {
    "fvg_inversion_tap": dict(
        cfg=dict(
            name="FVG Inversion Tap", instruments=["ES", "NQ"],
            primary_timeframe="15m", execution_timeframe="1m",
            higher_timeframes=["1H", "4H"], risk_reward_ratio=3.0,
            stop_loss_type="structure", stop_loss_ticks=None, max_contracts=10,
            session_filters=["NY_AM", "LONDON"], fvg_min_size_ticks=4,
            fvg_max_size_ticks=None, max_daily_loss=None, max_trades_per_day=5,
            use_rsi_filter=False, use_vwap_filter=False,
        ),
        breakeven_at_r=0.5, breakeven_mode="r",
    ),
    "judas_swing": dict(
        cfg=dict(
            name="Judas Swing", instruments=["ES", "NQ", "YM"],
            primary_timeframe="5m", execution_timeframe="1m",
            higher_timeframes=["1H"], risk_reward_ratio=3.0,
            stop_loss_type="structure", stop_loss_ticks=None, max_contracts=10,
            session_filters=["LONDON", "NY_AM"], fvg_min_size_ticks=4,
            fvg_max_size_ticks=None, max_daily_loss=None, max_trades_per_day=5,
            use_rsi_filter=False, use_vwap_filter=False,
        ),
        breakeven_at_r=1.0, breakeven_mode="structure",
    ),
}

_DATA_CACHE: dict = {}


def _load_bars(instrument: str):
    """Real 1m bars when the DB is reachable, else deterministic synthetic."""
    if instrument in _DATA_CACHE:
        return _DATA_CACHE[instrument]
    df, source = None, "synthetic"
    if os.environ.get("DATABASE_URL"):
        try:
            from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
            real = asyncio.run(fetch_futures_data(
                instrument=instrument, start_date=START, end_date=END,
                interval="1m", use_polygon=False,
            ))
            if real is not None and not real.empty:
                df, source = real.reset_index(), "real"
        except Exception as exc:  # unreachable DB -> synthetic fallback
            print(f"[parity] real-data fetch failed ({exc!r}); using synthetic")
    if df is None:
        from scripts.strategy_v2_harness import generate_synthetic_bars
        df = generate_synthetic_bars(START, END, seed=7, start_price=5000.0)
    _DATA_CACHE[instrument] = (df, source)
    return df, source


def _run_backtest(strategy_key: str, instrument: str, fast: bool):
    """Fresh strategy + handler + runner per run — mirrors the route."""
    spec = STRATEGIES[strategy_key]
    bars_df, source = _load_bars(instrument)

    config = StrategyConfig(**spec["cfg"])
    config.rule_tree = {}
    config.take_profit_mode = "auto"

    strategy = ICTStrategy(config, instrument=instrument)
    handler = DataHandler(instrument=instrument, base_timeframe=config.execution_timeframe)
    handler.load_from_dataframe(bars_df)  # load_from_dataframe copies

    all_tfs = list(set([config.primary_timeframe, config.execution_timeframe]
                       + config.higher_timeframes))
    bt_config = BacktestConfig(
        instrument=instrument, start_date=START, end_date=END,
        primary_timeframe=config.primary_timeframe, all_timeframes=all_tfs,
        initial_capital=100_000.0, commission_per_side=2.25, slippage_ticks=1,
        risk_per_trade_pct=1.0, trailing_drawdown=0.0, daily_loss_limit=0.0,
        breakeven_at_r=spec["breakeven_at_r"], breakeven_mode=spec["breakeven_mode"],
    )

    prev = os.environ.get("V2_FAST_BACKTEST")
    os.environ["V2_FAST_BACKTEST"] = "1" if fast else "0"
    try:
        runner = BacktestRunner(strategy, handler, bt_config)
        metrics = runner.run()
    finally:
        if prev is None:
            os.environ.pop("V2_FAST_BACKTEST", None)
        else:
            os.environ["V2_FAST_BACKTEST"] = prev

    # The flag must actually have armed/disarmed the fast path.
    assert strategy._fast_backtest is fast
    return runner.completed_trades, metrics, source


def _trade_row(t):
    return (
        t.instrument, t.direction, t.entry_time, t.exit_time,
        t.entry_price, t.exit_price, t.stop_loss, t.take_profit,
        t.contracts, t.pnl, t.pnl_ticks, t.commission, t.net_pnl,
        t.is_winner, t.exit_reason, t.be_trigger,
    )


def _metric_row(m):
    return {
        "total_trades": m.total_trades,
        "winning_trades": getattr(m, "winning_trades", None),
        "losing_trades": getattr(m, "losing_trades", None),
        "breakeven_trades": getattr(m, "breakeven_trades", None),
        "win_rate": m.win_rate,
        "effective_win_rate": getattr(m, "effective_win_rate", None),
        "net_profit": m.net_profit,
        "gross_profit": m.gross_profit,
        "gross_loss": m.gross_loss,
        "profit_factor": m.profit_factor,
        "max_drawdown": m.max_drawdown,
        "max_drawdown_pct": m.max_drawdown_pct,
        "sharpe_ratio": m.sharpe_ratio,
        "avg_rr": m.avg_rr,
        "equity_curve": m.equity_curve,
        "monthly_returns": m.monthly_returns,
    }


@pytest.mark.parametrize("strategy_key,instrument", [
    ("fvg_inversion_tap", "ES"),
    ("judas_swing", "ES"),
])
def test_fast_backtest_parity(strategy_key, instrument):
    slow_trades, slow_metrics, source = _run_backtest(strategy_key, instrument, fast=False)
    fast_trades, fast_metrics, _ = _run_backtest(strategy_key, instrument, fast=True)

    print(f"[parity] {strategy_key}/{instrument} data={source} "
          f"slow_trades={len(slow_trades)} fast_trades={len(fast_trades)}")

    # Identical ordered trade list: times, prices, stops/targets, size, pnl.
    slow_rows = [_trade_row(t) for t in slow_trades]
    fast_rows = [_trade_row(t) for t in fast_trades]
    assert len(fast_rows) == len(slow_rows), (
        f"trade count diverged: slow={len(slow_rows)} fast={len(fast_rows)}")
    for i, (s, f) in enumerate(zip(slow_rows, fast_rows)):
        assert s == f, f"trade #{i} diverged:\n  slow={s}\n  fast={f}"

    # Identical metrics (same trades -> same arithmetic, exact equality).
    assert _metric_row(fast_metrics) == _metric_row(slow_metrics)

    # A parity run that never trades proves nothing — require signal flow on
    # real data (the 2-week profile of this book produced 30 trades).
    if source == "real":
        assert len(slow_rows) > 0, "no trades on real data — parity check is vacuous"
