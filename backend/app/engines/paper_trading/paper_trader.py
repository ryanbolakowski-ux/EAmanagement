"""
Paper Trading Engine.
Uses real-time market data feeds but simulates order fills.
Tracks PnL, open positions, and session metrics identically to live trading.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional
from loguru import logger

from app.engines.strategy_engine.base_strategy import BaseStrategy, TradeSignal, SignalType, ExitReason

TICK_VALUES = {"ES": 12.50, "NQ": 5.00, "RTY": 5.00, "YM": 5.00}
TICK_SIZES  = {"ES": 0.25,  "NQ": 0.25, "RTY": 0.10, "YM": 1.0}


@dataclass
class PaperPosition:
    instrument: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    contracts: int
    entry_time: datetime
    metadata: dict = field(default_factory=dict)


@dataclass
class PaperTradeResult:
    instrument: str
    direction: str
    entry_price: float
    exit_price: float
    contracts: int
    entry_time: datetime
    exit_time: datetime
    pnl: float
    commission: float
    net_pnl: float
    is_winner: bool
    exit_reason: str
    metadata: dict = field(default_factory=dict)


class PaperTrader:
    """
    Paper trading engine that:
    - Subscribes to real-time tick/bar data
    - Calls strategy.on_bar() or strategy.on_tick() to get signals
    - Simulates fills immediately at market price
    - Monitors SL/TP and closes positions accordingly
    - Enforces risk controls (daily loss, max trades, kill switch)
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        instrument: str,
        commission_per_side: float = 2.25,
        session_id: Optional[str] = None,
    ):
        self.strategy    = strategy
        self.instrument  = instrument.upper()
        self.commission  = commission_per_side
        self.session_id  = session_id

        self._position: Optional[PaperPosition] = None
        self._completed_trades: list[PaperTradeResult] = []
        self._is_running: bool = False
        self._current_date: Optional[date] = None
        self._daily_pnl: float = 0.0
        self._daily_trades: int = 0
        self._kill_switch: bool = False

        self._bars_buffer: dict[str, list] = {}  # timeframe -> list of bar dicts

    # ─────────────────────────────────────────────────────────────────────────
    # Lifecycle
    # ─────────────────────────────────────────────────────────────────────────

    async def start(self):
        self._is_running = True
        self.strategy.reset_daily_counters()
        logger.info(f"[PaperTrader] Started | {self.instrument} | Strategy: {self.strategy.config.name}")

    async def stop(self):
        self._is_running = False
        if self._position:
            logger.warning("[PaperTrader] Stopping with open position — position left open in DB.")
        logger.info(f"[PaperTrader] Stopped | Trades: {len(self._completed_trades)} | PnL: ${self._daily_pnl:,.2f}")

    # ─────────────────────────────────────────────────────────────────────────
    # Data feed handlers (called by the data feed layer)
    # ─────────────────────────────────────────────────────────────────────────

    async def on_bar(self, timeframe: str, bar: dict):
        """Called when a new bar closes on any subscribed timeframe."""
        if not self._is_running or self._kill_switch:
            return

        # Buffer bar
        if timeframe not in self._bars_buffer:
            self._bars_buffer[timeframe] = []
        self._bars_buffer[timeframe].append(bar)
        if len(self._bars_buffer[timeframe]) > 500:
            self._bars_buffer[timeframe] = self._bars_buffer[timeframe][-500:]

        import pandas as pd
        bars_dict = {
            tf: pd.DataFrame(bars).set_index("timestamp")
            for tf, bars in self._bars_buffer.items()
            if bars
        }

        # Reset daily counters at day boundary
        ts = bar["timestamp"]
        if hasattr(ts, "date"):
            today = ts.date()
            if self._current_date != today:
                self._current_date = today
                self._daily_pnl    = 0.0
                self._daily_trades = 0
                self.strategy.reset_daily_counters()

        # Manage open position SL/TP
        if self._position:
            await self._check_position_exits(bar)

        # Look for entry signal
        if not self._position and self.strategy.check_risk_controls():
            signal = self.strategy.on_bar(bars_dict)
            if signal and signal.signal != SignalType.NONE:
                await self._open_position(signal, bar["timestamp"])

    async def on_tick(self, tick: dict):
        """Called on each incoming tick. Used for tighter exit management."""
        if not self._is_running or self._kill_switch or not self._position:
            return
        await self._check_position_exits_on_tick(tick)

    # ─────────────────────────────────────────────────────────────────────────
    # Position management
    # ─────────────────────────────────────────────────────────────────────────

    async def _open_position(self, signal: TradeSignal, timestamp):
        self._position = PaperPosition(
            instrument=self.instrument,
            direction=signal.signal.value,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            contracts=signal.contracts,
            entry_time=timestamp if isinstance(timestamp, datetime) else timestamp.to_pydatetime(),
            metadata=signal.metadata,
        )
        logger.info(f"[Paper] OPEN {signal.signal.value.upper()} @ {signal.entry_price:.2f} | SL={signal.stop_loss:.2f} | TP={signal.take_profit:.2f}")

    async def _check_position_exits(self, bar: dict):
        p = self._position
        if not p:
            return
        hit_tp = hit_sl = False
        if p.direction == "long":
            if bar["low"] <= p.stop_loss:
                hit_sl = True
            elif bar["high"] >= p.take_profit:
                hit_tp = True
        else:
            if bar["high"] >= p.stop_loss:
                hit_sl = True
            elif bar["low"] <= p.take_profit:
                hit_tp = True

        if hit_tp or hit_sl:
            exit_price = p.take_profit if hit_tp else p.stop_loss
            reason = ExitReason.TP_HIT if hit_tp else ExitReason.SL_HIT
            await self._close_position(exit_price, bar["timestamp"], reason)

    async def _check_position_exits_on_tick(self, tick: dict):
        p = self._position
        if not p:
            return
        price = tick["price"]
        hit_tp = hit_sl = False
        if p.direction == "long":
            if price <= p.stop_loss:
                hit_sl = True
            elif price >= p.take_profit:
                hit_tp = True
        else:
            if price >= p.stop_loss:
                hit_sl = True
            elif price <= p.take_profit:
                hit_tp = True

        if hit_tp or hit_sl:
            exit_price = p.take_profit if hit_tp else p.stop_loss
            reason = ExitReason.TP_HIT if hit_tp else ExitReason.SL_HIT
            await self._close_position(exit_price, tick["timestamp"], reason)

    async def _close_position(self, exit_price: float, exit_time, reason: ExitReason):
        p = self._position
        if not p:
            return

        tick_size  = TICK_SIZES.get(self.instrument, 0.25)
        tick_value = TICK_VALUES.get(self.instrument, 12.50)

        if p.direction == "long":
            pnl_ticks = (exit_price - p.entry_price) / tick_size
        else:
            pnl_ticks = (p.entry_price - exit_price) / tick_size

        pnl       = pnl_ticks * tick_value * p.contracts
        commission = self.commission * 2 * p.contracts
        net_pnl   = pnl - commission
        is_winner = net_pnl > 0

        result = PaperTradeResult(
            instrument=p.instrument,
            direction=p.direction,
            entry_price=p.entry_price,
            exit_price=exit_price,
            contracts=p.contracts,
            entry_time=p.entry_time,
            exit_time=exit_time if isinstance(exit_time, datetime) else exit_time.to_pydatetime(),
            pnl=pnl,
            commission=commission,
            net_pnl=net_pnl,
            is_winner=is_winner,
            exit_reason=reason.value,
            metadata=p.metadata,
        )

        self._completed_trades.append(result)
        self._daily_pnl    += net_pnl
        self._daily_trades += 1
        self.strategy.record_trade_result(net_pnl)

        logger.info(f"[Paper] CLOSE {reason.value} @ {exit_price:.2f} | Net PnL: ${net_pnl:,.2f} | {'WIN' if is_winner else 'LOSS'}")
        self._position = None

    def trigger_kill_switch(self):
        self._kill_switch = True
        self.strategy.trigger_kill_switch()
        logger.warning("[PaperTrader] KILL SWITCH TRIGGERED — no new trades will be placed.")

    @property
    def stats(self) -> dict:
        trades = self._completed_trades
        total  = len(trades)
        wins   = sum(1 for t in trades if t.is_winner)
        return {
            "total_trades": total,
            "win_rate":     (wins / total) if total else 0.0,
            "net_pnl":      sum(t.net_pnl for t in trades),
            "daily_pnl":    self._daily_pnl,
            "is_running":   self._is_running,
            "kill_switch":  self._kill_switch,
            "open_position": bool(self._position),
        }
