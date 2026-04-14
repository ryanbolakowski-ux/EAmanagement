"""
Backtest Runner — the core simulation engine.
Iterates bar-by-bar over historical data, feeds the strategy,
simulates order fills with slippage and commission, and records trades.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import pandas as pd
from loguru import logger

from app.engines.strategy_engine.base_strategy import BaseStrategy, TradeSignal, SignalType, ExitReason
from app.engines.backtest_engine.data_handler import DataHandler
from app.engines.backtest_engine.metrics import BacktestMetricsResult, calculate_metrics


TICK_VALUES = {
    "ES": 12.50,   # $12.50 per tick ($50/point, 0.25 tick)
    "NQ": 5.00,    # $5.00 per tick ($20/point, 0.25 tick)
    "RTY": 5.00,
    "YM": 5.00,
}

TICK_SIZES = {
    "ES": 0.25,
    "NQ": 0.25,
    "RTY": 0.10,
    "YM": 1.0,
}


@dataclass
class SimulatedTrade:
    instrument: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    contracts: int
    entry_time: datetime
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    pnl: float = 0.0
    pnl_ticks: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0
    net_pnl: float = 0.0
    is_winner: bool = False
    exit_reason: str = ""
    conditions_snapshot: dict = field(default_factory=dict)


@dataclass
class BacktestConfig:
    instrument: str
    start_date: datetime
    end_date: datetime
    primary_timeframe: str
    all_timeframes: list[str]
    initial_capital: float = 100_000.0
    commission_per_side: float = 2.25   # per contract
    slippage_ticks: int = 1


class BacktestRunner:

    def __init__(self, strategy: BaseStrategy, data_handler: DataHandler, config: BacktestConfig):
        self.strategy = strategy
        self.data_handler = data_handler
        self.config = config
        self._open_trade: Optional[SimulatedTrade] = None
        self._completed_trades: list[SimulatedTrade] = []
        self._current_date: Optional[datetime] = None

    def run(self) -> BacktestMetricsResult:
        instrument = self.config.instrument
        tick_size  = TICK_SIZES.get(instrument, 0.25)
        tick_value = TICK_VALUES.get(instrument, 12.50)

        # Build all timeframes
        self.data_handler.build_timeframes(self.config.all_timeframes)
        self.data_handler.filter_date_range(
            pd.Timestamp(self.config.start_date, tz="UTC"),
            pd.Timestamp(self.config.end_date, tz="UTC"),
        )

        primary_bars = self.data_handler.get_timeframe_bars(self.config.primary_timeframe)
        logger.info(f"Starting backtest: {len(primary_bars)} primary bars ({self.config.primary_timeframe})")

        self.strategy.reset_daily_counters()

        for i, (timestamp, _) in enumerate(primary_bars.iterrows()):
            # Reset daily counters at day boundary
            if self._current_date != timestamp.date():
                self._current_date = timestamp.date()
                self.strategy.reset_daily_counters()

            current_bar = primary_bars.iloc[i]

            # ── Manage open trade exits (check SL/TP on each bar) ─────────────
            if self._open_trade:
                self._check_exits(current_bar, timestamp, tick_size, tick_value)

            # ── Only look for new signals if no open trade ────────────────────
            if not self._open_trade:
                bars = self.data_handler.get_bars_up_to(timestamp, self.config.all_timeframes)
                signal: Optional[TradeSignal] = self.strategy.on_bar(bars)

                if signal and signal.signal != SignalType.NONE:
                    entry = self._apply_slippage(signal.entry_price, signal.signal.value, tick_size)
                    self._open_trade = SimulatedTrade(
                        instrument=instrument,
                        direction=signal.signal.value,
                        entry_price=entry,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                        contracts=signal.contracts,
                        entry_time=timestamp.to_pydatetime(),
                        conditions_snapshot=signal.metadata,
                    )
                    logger.debug(f"  ENTRY {signal.signal.value.upper()} @ {entry:.2f} | SL={signal.stop_loss:.2f} | TP={signal.take_profit:.2f}")

        # Close any trade still open at end of data
        if self._open_trade:
            last_bar = primary_bars.iloc[-1]
            last_ts  = primary_bars.index[-1]
            self._force_close_trade(float(last_bar["close"]), last_ts.to_pydatetime(), tick_size, tick_value)

        # Build metrics
        trade_dicts = [
            {
                "entry_time": t.entry_time,
                "exit_time":  t.exit_time,
                "net_pnl":    t.net_pnl,
                "is_winner":  t.is_winner,
            }
            for t in self._completed_trades
        ]
        metrics = calculate_metrics(trade_dicts, self.config.initial_capital)
        logger.info(f"Backtest complete: {metrics.total_trades} trades | WR={metrics.win_rate:.1%} | PF={metrics.profit_factor:.2f} | Net P&L=${metrics.net_profit:,.0f}")
        return metrics

    # ─────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _apply_slippage(self, price: float, direction: str, tick_size: float) -> float:
        slip = self.config.slippage_ticks * tick_size
        return price + slip if direction == "long" else price - slip

    def _check_exits(self, bar: pd.Series, timestamp: pd.Timestamp, tick_size: float, tick_value: float):
        t = self._open_trade
        hit_tp = hit_sl = False

        if t.direction == "long":
            if bar["low"] <= t.stop_loss:
                hit_sl = True
            elif bar["high"] >= t.take_profit:
                hit_tp = True
        else:
            if bar["high"] >= t.stop_loss:
                hit_sl = True
            elif bar["low"] <= t.take_profit:
                hit_tp = True

        if hit_tp or hit_sl:
            exit_price = t.take_profit if hit_tp else t.stop_loss
            exit_price = self._apply_slippage(exit_price, "short" if t.direction == "long" else "long", tick_size)
            self._close_trade(exit_price, timestamp.to_pydatetime(), tick_size, tick_value,
                              ExitReason.TP_HIT if hit_tp else ExitReason.SL_HIT)

    def _close_trade(self, exit_price: float, exit_time: datetime, tick_size: float, tick_value: float, reason: ExitReason):
        t = self._open_trade
        if t is None:
            return
        t.exit_price  = exit_price
        t.exit_time   = exit_time
        t.exit_reason = reason.value

        if t.direction == "long":
            t.pnl_ticks = (exit_price - t.entry_price) / tick_size
        else:
            t.pnl_ticks = (t.entry_price - exit_price) / tick_size

        t.pnl = t.pnl_ticks * tick_value * t.contracts
        t.commission = self.config.commission_per_side * 2 * t.contracts  # round trip
        t.net_pnl  = t.pnl - t.commission
        t.is_winner = t.net_pnl > 0

        self.strategy.record_trade_result(t.net_pnl)
        self._completed_trades.append(t)
        self._open_trade = None
        logger.debug(f"  EXIT {reason.value} @ {exit_price:.2f} | Net P&L=${t.net_pnl:,.2f}")

    def _force_close_trade(self, exit_price: float, exit_time: datetime, tick_size: float, tick_value: float):
        self._close_trade(exit_price, exit_time, tick_size, tick_value, ExitReason.SESSION_END)

    @property
    def completed_trades(self) -> list[SimulatedTrade]:
        return self._completed_trades
