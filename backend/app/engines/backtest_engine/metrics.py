"""
Backtest performance metrics calculator.
Computes all output metrics from a list of completed trades.
"""
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np


@dataclass
class BacktestMetricsResult:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    breakeven_trades: int = 0
    win_rate: float = 0.0
    # Effective win rate excludes break-even exits from the denominator —
    # answers "of trades that took a real outcome, what % won?" Much more
    # meaningful when the break-even-at-1R rule is active.
    effective_win_rate: float = 0.0

    net_profit: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    profit_factor: float = 0.0

    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: Optional[float] = None
    sortino_ratio: Optional[float] = None

    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_rr: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0

    avg_trade_duration_minutes: float = 0.0
    equity_curve: list = field(default_factory=list)
    monthly_returns: dict = field(default_factory=dict)



def win_rate_stats(winning_trades: int, losing_trades: int, breakeven_trades: int) -> dict:
    """CANONICAL win-rate definitions — the SINGLE source of truth shared by every
    metrics path (futures backtest, optimizer, options backtest). Keeping the math
    in one place is what guarantees the strategy database, builder, optimizer and
    backtest can never drift to different win-rate definitions.

    Conventions (documented + asserted):
      * `winning_trades` INCLUDES break-even exits — a scratch at entry is a
        non-loss, so it rolls into wins for the headline win_rate.
      * total              = winning_trades + losing_trades   (BE lives inside wins)
      * win_rate           = winning_trades / total           (BE counts as a non-loss)
      * effective_win_rate = real_wins / (real_wins + losses) (BE excluded entirely)
        where real_wins = winning_trades - breakeven_trades.

    The two rates answer different questions and BOTH are surfaced in the UI:
      win_rate          -> "how often did I avoid a full loss?"  (BE = scratch)
      effective_win_rate-> "of trades that fully resolved, how many hit target?"
    """
    winning_trades = int(winning_trades or 0)
    losing_trades = int(losing_trades or 0)
    breakeven_trades = int(breakeven_trades or 0)
    # Invariants — break loudly rather than silently report inconsistent stats.
    assert breakeven_trades >= 0 and winning_trades >= 0 and losing_trades >= 0
    assert breakeven_trades <= winning_trades, (
        f"breakeven_trades ({breakeven_trades}) cannot exceed winning_trades ({winning_trades})")
    total = winning_trades + losing_trades
    real_wins = winning_trades - breakeven_trades
    decisive = real_wins + losing_trades
    return {
        "total_trades": total,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "breakeven_trades": breakeven_trades,
        "real_wins": real_wins,
        "win_rate": (winning_trades / total) if total else 0.0,
        "effective_win_rate": (real_wins / decisive) if decisive else 0.0,
    }


def calculate_metrics(
    trades: list[dict],
    initial_capital: float = 100_000.0,
    risk_free_rate_annual: float = 0.05,
) -> BacktestMetricsResult:
    """
    `trades` is a list of dicts:
    {
        "entry_time": datetime,
        "exit_time": datetime,
        "net_pnl": float,
        "is_winner": bool,
    }
    """
    m = BacktestMetricsResult()
    if not trades:
        return m

    m.total_trades = len(trades)
    # BE exits are flagged is_winner=True at the runner level, so they roll
    # up into 'wins' naturally — matches the user's intent: 'BE counts as win'.
    breakevens = [t for t in trades if t.get("exit_reason") == "breakeven"]
    wins       = [t for t in trades if t.get("is_winner")]
    losses     = [t for t in trades if not t.get("is_winner")]

    # Route every win-rate number through the ONE canonical helper so the
    # backtest, optimizer and options paths can never define win rate
    # differently. (see win_rate_stats docstring for the conventions.)
    _wr = win_rate_stats(len(wins), len(losses), len(breakevens))
    m.winning_trades     = _wr["winning_trades"]   # includes BE
    m.losing_trades      = _wr["losing_trades"]
    m.breakeven_trades   = _wr["breakeven_trades"]
    m.win_rate           = _wr["win_rate"]
    m.effective_win_rate = _wr["effective_win_rate"]

    pnls = [t["net_pnl"] for t in trades]
    m.net_profit  = sum(pnls)
    m.gross_profit = sum(p for p in pnls if p > 0)
    m.gross_loss   = abs(sum(p for p in pnls if p < 0))
    m.profit_factor = (m.gross_profit / m.gross_loss) if m.gross_loss > 0 else float("inf")

    m.avg_win  = (m.gross_profit / m.winning_trades)  if m.winning_trades else 0.0
    m.avg_loss = -(m.gross_loss  / m.losing_trades)   if m.losing_trades  else 0.0
    m.avg_rr   = abs(m.avg_win / m.avg_loss) if m.avg_loss else 0.0

    m.largest_win  = max(pnls) if pnls else 0.0
    m.largest_loss = min(pnls) if pnls else 0.0

    # Equity curve & drawdown
    equity = initial_capital
    peak   = initial_capital
    equity_curve = []
    drawdowns = []

    for t in sorted(trades, key=lambda x: x["entry_time"]):
        equity += t["net_pnl"]
        equity_curve.append({"timestamp": t["exit_time"].isoformat(), "equity": round(equity, 2)})
        if equity > peak:
            peak = equity
        dd = peak - equity
        dd_pct = (dd / peak * 100) if peak > 0 else 0.0
        drawdowns.append(dd)
        if dd > m.max_drawdown:
            m.max_drawdown     = dd
            m.max_drawdown_pct = dd_pct

    m.equity_curve = equity_curve

    # Sharpe / Sortino (daily returns)
    if len(pnls) >= 5:
        returns = np.array(pnls) / initial_capital
        mean_r  = np.mean(returns)
        std_r   = np.std(returns)
        rf_daily = risk_free_rate_annual / 252

        if std_r > 0:
            m.sharpe_ratio = float((mean_r - rf_daily) / std_r * np.sqrt(252))
        downside = returns[returns < 0]
        if len(downside) > 0:
            downside_std = np.std(downside)
            if downside_std > 0:
                m.sortino_ratio = float((mean_r - rf_daily) / downside_std * np.sqrt(252))

    # Average trade duration
    durations = []
    for t in trades:
        try:
            dur = (t["exit_time"] - t["entry_time"]).total_seconds() / 60
            durations.append(dur)
        except Exception:
            pass
    if durations:
        m.avg_trade_duration_minutes = float(np.mean(durations))

    # Monthly returns
    monthly: dict[str, float] = {}
    for t in trades:
        key = t["entry_time"].strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0.0) + t["net_pnl"]
    m.monthly_returns = monthly

    return m
