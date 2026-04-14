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
    win_rate: float = 0.0

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
    wins   = [t for t in trades if t["is_winner"]]
    losses = [t for t in trades if not t["is_winner"]]

    m.winning_trades = len(wins)
    m.losing_trades  = len(losses)
    m.win_rate       = m.winning_trades / m.total_trades if m.total_trades else 0.0

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
