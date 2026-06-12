"""Proof: backtest summary metrics are computed consistently from the trade list."""
from datetime import datetime, timedelta
from app.engines.backtest_engine.metrics import calculate_metrics

def _t(net, winner, reason, i):
    t0 = datetime(2026, 1, 1) + timedelta(hours=i)
    return {"net_pnl": net, "is_winner": winner, "exit_reason": reason,
            "entry_time": t0, "exit_time": t0 + timedelta(minutes=30)}

def test_metrics_match_trade_list():
    trades = ([_t(100, True, "tp_hit", i) for i in range(6)] +       # 6 TP wins
              [_t(-50, False, "sl_hit", 10+i) for i in range(3)] +   # 3 SL losses
              [_t(0, True, "breakeven", 20)])                         # 1 breakeven (counts as win)
    m = calculate_metrics(trades, 100000)
    assert m.total_trades == 10, m.total_trades
    assert m.winning_trades == 7, m.winning_trades        # 6 tp + 1 be
    assert m.losing_trades == 3, m.losing_trades
    assert m.breakeven_trades == 1, m.breakeven_trades
    assert abs(m.win_rate - 0.7) < 1e-9, m.win_rate        # 7/10
    assert abs(m.effective_win_rate - (6/9)) < 1e-9, m.effective_win_rate  # excludes BE
    assert abs(m.profit_factor - 4.0) < 1e-9, m.profit_factor  # 600/150
    assert abs(m.net_profit - 450) < 1e-9, m.net_profit
    # the invariant the storage bug violated: counts reconcile to the rate
    assert m.winning_trades + m.losing_trades == m.total_trades
    assert abs(m.win_rate - m.winning_trades / m.total_trades) < 1e-9

def test_all_wins_all_losses():
    m = calculate_metrics([_t(10, True, "tp_hit", i) for i in range(4)], 100000)
    assert m.win_rate == 1.0 and m.winning_trades == 4 and m.losing_trades == 0
    m2 = calculate_metrics([_t(-10, False, "sl_hit", i) for i in range(4)], 100000)
    assert m2.win_rate == 0.0 and m2.winning_trades == 0 and m2.losing_trades == 4

def test_empty():
    m = calculate_metrics([], 100000)
    assert m.total_trades == 0 and m.win_rate == 0.0
