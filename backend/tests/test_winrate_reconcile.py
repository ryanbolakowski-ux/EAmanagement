"""Win-rate reconciliation tests.

Locks the things the FVG 47%-vs-70% investigation fixed:
  1. ONE canonical win-rate definition (win_rate_stats) — BE = non-loss in
     win_rate, excluded from effective_win_rate; invariants enforced.
  2. calculate_metrics routes through it and its counts reconcile exactly.
  3. Runner exit determinism: same-candle stop/target tie -> SL (documented);
     structure-based break-even arms on the prior-swing break, then scratches
     at entry (counted as a non-loss), and is ignored when mode is 'off'.
"""
import pytest
import pandas as pd
from datetime import datetime
from app.engines.backtest_engine.metrics import win_rate_stats, calculate_metrics
from app.engines.backtest_engine.backtest_runner import (
    BacktestRunner, BacktestConfig, SimulatedTrade, ExitReason,
)


# ── 1) canonical helper ──────────────────────────────────────────────────────
def test_win_rate_stats_definitions():
    r = win_rate_stats(winning_trades=80, losing_trades=20, breakeven_trades=14)
    assert r["total_trades"] == 100
    assert r["real_wins"] == 66
    assert r["win_rate"] == pytest.approx(0.80)            # BE counts as a non-loss
    assert r["effective_win_rate"] == pytest.approx(66 / 86)  # BE excluded entirely

def test_win_rate_stats_no_breakeven_makes_rates_equal():
    r = win_rate_stats(47, 53, 0)
    assert r["win_rate"] == pytest.approx(0.47)
    assert r["effective_win_rate"] == pytest.approx(0.47)

def test_win_rate_stats_invariant_breakeven_exceeds_wins():
    with pytest.raises(AssertionError):
        win_rate_stats(5, 1, 9)        # impossible: more BE than wins

def test_win_rate_stats_empty():
    r = win_rate_stats(0, 0, 0)
    assert r["win_rate"] == 0.0 and r["effective_win_rate"] == 0.0


# ── 2) calculate_metrics reconciles to the helper ────────────────────────────
def _t(net, reason, is_winner, i):
    return {"entry_time": datetime(2026, 1, 1, 9, i % 59),
            "exit_time": datetime(2026, 1, 1, 10, i % 59),
            "net_pnl": net, "is_winner": is_winner, "exit_reason": reason}

def test_calculate_metrics_counts_reconcile():
    trades = []
    for i in range(30): trades.append(_t(500.0, "tp_hit", True, i))     # real wins
    for i in range(14): trades.append(_t(-5.0, "breakeven", True, i))   # BE (non-loss)
    for i in range(56): trades.append(_t(-300.0, "sl_hit", False, i))   # losses
    m = calculate_metrics(trades, 100_000)
    assert m.total_trades == 100
    assert m.winning_trades + m.losing_trades == m.total_trades   # BE folded into wins
    assert m.breakeven_trades == 14
    assert m.winning_trades == 44 and m.losing_trades == 56
    assert m.win_rate == pytest.approx(0.44)
    assert m.effective_win_rate == pytest.approx(30 / 86)
    r = win_rate_stats(m.winning_trades, m.losing_trades, m.breakeven_trades)
    assert m.win_rate == pytest.approx(r["win_rate"])
    assert m.effective_win_rate == pytest.approx(r["effective_win_rate"])


# ── 3) runner exit determinism + structure break-even ────────────────────────
class _StubStrategy:
    def on_bar(self, bars): return None
    def record_trade_result(self, pnl): pass

def _runner(mode="off"):
    cfg = BacktestConfig(instrument="NQ", start_date=datetime(2025, 1, 1),
                         end_date=datetime(2026, 1, 1), primary_timeframe="1m",
                         all_timeframes=["1m"], breakeven_mode=mode,
                         commission_per_side=0.0, slippage_ticks=0)
    return BacktestRunner(_StubStrategy(), object(), cfg)

def _bar(low, high):
    return pd.Series({"open": (low + high) / 2, "high": high, "low": low, "close": (low + high) / 2})

def _long(**kw):
    base = dict(instrument="NQ", direction="long", entry_price=100.0, stop_loss=99.0,
                take_profit=110.0, contracts=1, entry_time=datetime(2026, 1, 1, 9, 30))
    base.update(kw)
    return SimulatedTrade(**base)

def test_same_candle_tie_resolves_to_stop():
    r = _runner("off")
    r._open_trade = _long(take_profit=103.0)
    r._check_exits(_bar(low=99.0, high=103.0), pd.Timestamp("2026-01-01 09:31"), 0.25, 0.5)
    assert r._open_trade is None
    closed = r._completed_trades[-1]
    assert closed.exit_reason == ExitReason.SL_HIT.value      # ties -> stop, deterministic
    assert closed.is_winner is False

def test_structure_breakeven_arms_then_scratches():
    r = _runner("structure")
    r._open_trade = _long(be_trigger=101.0)
    # bar reaches the prior-swing trigger (101) but not stop/tp -> stop slides to entry
    r._check_exits(_bar(low=100.5, high=101.5), pd.Timestamp("2026-01-01 09:31"), 0.25, 0.5)
    assert r._open_trade is not None
    assert r._open_trade.stop_loss == 100.0
    # next bar pulls back to entry -> BE scratch, counted as a non-loss
    r._check_exits(_bar(low=99.5, high=100.5), pd.Timestamp("2026-01-01 09:32"), 0.25, 0.5)
    assert r._open_trade is None
    closed = r._completed_trades[-1]
    assert closed.exit_reason == ExitReason.BREAKEVEN.value
    assert closed.is_winner is True

def test_off_mode_ignores_be_trigger():
    r = _runner("off")
    r._open_trade = _long(be_trigger=101.0)
    r._check_exits(_bar(low=100.5, high=101.5), pd.Timestamp("2026-01-01 09:31"), 0.25, 0.5)
    assert r._open_trade is not None
    assert r._open_trade.stop_loss == 99.0   # untouched: no break-even when mode is off
