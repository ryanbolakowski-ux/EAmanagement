"""Backtest must scale with account size WITHOUT changing strategy behavior.

- Dollar P&L scales with capital.
- Trade count, win rate, drawdown %, avg R, profit factor stay consistent.

The fast unit test pins the core invariant (proportional sizing, identical
risk %, no size-dependent trade eligibility). The integration test runs the
same NQ strategy at $100k and $1M over a real data window and asserts the
ratio metrics match while dollars scale.
"""
import asyncio
from datetime import datetime, timezone, timedelta
import pytest

from app.engines.backtest_engine.backtest_runner import BacktestRunner, BacktestConfig
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig
from app.engines.backtest_engine.data_handler import DataHandler

NQ_TICK = 0.25
NQ_TICK_VALUE = 5.0  # $/tick for NQ mini


def _cfg():
    return StrategyConfig(name="scale", instruments=["NQ"], primary_timeframe="15m",
                          execution_timeframe="1m", higher_timeframes=["1H"],
                          risk_reward_ratio=2.0, stop_loss_type="structure", max_contracts=50)


def _runner(capital, compounding=False):
    btc = BacktestConfig(instrument="NQ",
                         start_date=datetime.now(timezone.utc) - timedelta(days=1),
                         end_date=datetime.now(timezone.utc), primary_timeframe="15m",
                         all_timeframes=["1m", "15m", "1H"], initial_capital=capital,
                         commission_per_side=2.50, risk_per_trade_pct=1.0,
                         max_contracts_cap=100, compounding=compounding)
    return BacktestRunner(ICTStrategy(_cfg(), instrument="NQ"),
                          DataHandler(instrument="NQ", base_timeframe="1m"), btc)


def _risk_pct(n, entry, stop, capital):
    loss_per = abs(entry - stop) / NQ_TICK * NQ_TICK_VALUE + 2.50 * 2
    return n * loss_per / capital * 100


def test_position_sizing_scales_proportionally():
    """A 50-pt NQ stop: $100k risks 1% on 1 contract; $1M risks 1% on 10."""
    entry, stop = 20000.0, 19950.0
    n100 = _runner(100_000)._pick_contract_size(entry, stop, NQ_TICK, NQ_TICK_VALUE, 50)
    n1m = _runner(1_000_000)._pick_contract_size(entry, stop, NQ_TICK, NQ_TICK_VALUE, 50)
    assert n100 == 1, n100
    assert n1m == 10, n1m
    # identical per-trade risk % is what keeps drawdown %, win rate, R consistent
    assert abs(_risk_pct(n100, entry, stop, 100_000) - _risk_pct(n1m, entry, stop, 1_000_000)) < 0.05


def test_wide_stop_trade_not_skipped_on_small_account():
    """Eligibility must not depend on account size: a wide-stop trade the old
    code skipped on $100k (contracts floored to 0) is now taken (>=1)."""
    entry, stop = 20000.0, 19700.0  # 300-pt stop -> $6000/contract risk
    n100 = _runner(100_000)._pick_contract_size(entry, stop, NQ_TICK, NQ_TICK_VALUE, 50)
    n1m = _runner(1_000_000)._pick_contract_size(entry, stop, NQ_TICK, NQ_TICK_VALUE, 50)
    assert n100 >= 1, "small account must still take the trade (min 1 contract)"
    assert n1m >= 1


def test_compounding_flag_defaults_off():
    """Default sizing is off initial capital (deterministic), not current equity."""
    r = _runner(100_000)
    assert r.config.compounding is False


# ── Integration: full backtest at two account sizes over real data ──
@pytest.fixture(scope="module")
def nq_data_handler():
    from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
    end = datetime(2026, 5, 27, tzinfo=timezone.utc)
    start = end - timedelta(days=90)

    async def fetch():
        return await fetch_futures_data(instrument="NQ", start_date=start, end_date=end,
                                        interval="1m", use_polygon=True)
    df = asyncio.run(fetch())
    if df is None or df.empty:
        pytest.skip("no NQ data available")
    dh = DataHandler(instrument="NQ", base_timeframe="1m")
    dh.load_from_dataframe(df.reset_index())
    dh.build_timeframes(["1m", "15m", "1H"])
    return dh, start, end


def _run_full(dh, start, end, capital):
    btc = BacktestConfig(instrument="NQ", start_date=start, end_date=end,
                         primary_timeframe="15m", all_timeframes=["1m", "15m", "1H"],
                         initial_capital=capital, commission_per_side=2.50, slippage_ticks=1,
                         risk_per_trade_pct=1.0, max_contracts_cap=100, compounding=False)
    return BacktestRunner(ICTStrategy(_cfg(), instrument="NQ"), dh, btc).run()


def test_backtest_metrics_consistent_across_account_size(nq_data_handler):
    dh, start, end = nq_data_handler
    a = _run_full(dh, start, end, 100_000)
    b = _run_full(dh, start, end, 1_000_000)

    # Same trades taken regardless of account size
    assert a.total_trades == b.total_trades, f"{a.total_trades} vs {b.total_trades}"
    if a.total_trades == 0:
        pytest.skip("strategy produced no trades on this window")
    # Win rate is size-independent (same trades, same outcomes) -> identical.
    assert abs(a.win_rate - b.win_rate) < 0.005, f"WR {a.win_rate} vs {b.win_rate}"
    # DD% / avg R are nearly identical; the small residual is integer-contract
    # rounding (a $100k account can't size fractional minis). The original bug
    # produced a ~3x (5.7pp) DD divergence; require it within 2.5pp now.
    assert abs(a.max_drawdown_pct - b.max_drawdown_pct) < 2.5, f"DD% {a.max_drawdown_pct} vs {b.max_drawdown_pct}"
    assert abs(a.avg_rr - b.avg_rr) < 0.25, f"avgR {a.avg_rr} vs {b.avg_rr}"
    # Dollars scale meaningfully with account size (was ~1.44x before the fix;
    # rounding keeps it sublinear vs a perfect 10x, but clearly proportional).
    if a.net_profit > 0:
        ratio = b.net_profit / a.net_profit
        assert 3.0 <= ratio <= 12.0, f"net P&L should scale with size, got {ratio:.2f}x"
