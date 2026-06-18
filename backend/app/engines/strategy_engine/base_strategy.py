"""
Base Strategy class — all user-defined strategies inherit from this.
Provides the contract that backtesting, paper trading, and live trading engines all consume.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import pandas as pd


class SignalType(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class ExitReason(str, Enum):
    TP_HIT = "tp_hit"
    SL_HIT = "sl_hit"
    BREAKEVEN = "breakeven"  # stop was moved to entry and triggered there
    MANUAL = "manual"
    SESSION_END = "session_end"
    KILL_SWITCH = "kill_switch"


@dataclass
class TradeSignal:
    signal: SignalType
    instrument: str
    entry_price: float
    stop_loss: float
    take_profit: float
    contracts: int = 1
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: dict = field(default_factory=dict)  # e.g. {"fvg_high": 5120.50, "sweep_level": 5115.00}


@dataclass
class StrategyConfig:
    name: str
    instruments: list[str]
    primary_timeframe: str = "15m"
    execution_timeframe: str = "1m"
    higher_timeframes: list[str] = field(default_factory=list)
    risk_reward_ratio: float = 2.0
    stop_loss_type: str = "structure"       # "ticks" or "structure"
    # RANGE-TP-V1: "auto" (existing swing/HTF-FVG/RR hierarchy) or "range"
    # (target the opposite extreme of the swept dealing range).
    take_profit_mode: str = "auto"
    stop_loss_ticks: Optional[int] = None
    max_contracts: int = 1
    session_filters: list[str] = field(default_factory=list)
    fvg_min_size_ticks: int = 4
    fvg_max_size_ticks: Optional[int] = None
    max_daily_loss: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    # Optional confirmation filters. When True, the engine vetoes entries
    # that don't pass the corresponding indicator check.
    use_rsi_filter: bool = False
    rsi_period: int = 14
    rsi_long_max: float = 70.0    # block longs when RSI is overheated
    rsi_short_min: float = 30.0   # block shorts when RSI is oversold
    use_vwap_filter: bool = False
    # RULE-TREE-PLUMB-V1: the compiled/declared rule tree (jsonb on the
    # strategy). Carries engine_version ('v1'/'v2'), ict_setup id, and any
    # Plain-English-compiler primitives. Must be set for the v2 dispatch
    # and dedicated setups to run; default {} keeps v1 behaviour unchanged.
    rule_tree: dict = field(default_factory=dict)

    # ── Options-specific config (used by the options engine, ignored elsewhere) ──
    # The user-provided framework defines five swing-options modes:
    #   trend_pullback | breakout | vertical_spread | earnings_catalyst | wheel
    options_mode: Optional[str] = None
    # Position sizing as a % of account equity (1-2% per the user's rules)
    options_risk_per_trade_pct: float = 1.5
    # Days to expiration: at least 30 to minimize theta decay
    options_min_dte: int = 30
    options_max_dte: int = 60
    # Delta band for strike selection. 0.30-0.50 = balance of leverage and decay,
    # 0.55+ = more ITM (safer, less leverage), set higher when prefer_itm is on.
    options_target_delta_min: float = 0.30
    options_target_delta_max: float = 0.50
    options_prefer_itm: bool = False
    # Vertical-spread width in strikes (e.g. 5 = sell strike +5 above the long leg)
    options_spread_width: int = 5
    # Breakout strategy: require today's volume be at least Nx the 20-day average
    options_breakout_volume_mult: float = 2.0
    # Skip trades when earnings are within N days (default 7), unless mode is
    # earnings_catalyst (which DEPENDS on earnings being near).
    options_avoid_earnings_days: int = 7


class BaseStrategy(ABC):
    """
    Every strategy must implement:
      - on_bar(bars): called on each new candle close across all timeframes
      - on_tick(tick): called on each tick (for live/paper tick-level execution)

    The engine calls these methods and expects a TradeSignal back (or None).
    """

    def __init__(self, config: StrategyConfig):
        self.config = config
        self._daily_trade_count: int = 0
        self._daily_pnl: float = 0.0
        self._kill_switch: bool = False

    # -------------------------------------------------------------------------
    # Abstract interface
    # -------------------------------------------------------------------------

    @abstractmethod
    def on_bar(self, bars: dict[str, pd.DataFrame]) -> Optional[TradeSignal]:
        """
        Called on each bar close. `bars` is a dict of timeframe -> OHLCV DataFrame.
        Return a TradeSignal to open a trade, or None to skip.

        Example keys: {"1m": df_1m, "15m": df_15m, "1H": df_1H}
        Each DataFrame has columns: [open, high, low, close, volume, timestamp]
        """
        ...

    @abstractmethod
    def on_tick(self, tick: dict) -> Optional[TradeSignal]:
        """
        Called on each tick for live/paper execution.
        tick = {"instrument": "ES", "price": 5120.25, "timestamp": datetime, "volume": 10}
        """
        ...

    # -------------------------------------------------------------------------
    # Risk control hooks (called by engine before sending any signal)
    # -------------------------------------------------------------------------

    def check_risk_controls(self) -> bool:
        """Returns False if any risk limit is breached and trade should be blocked."""
        if self._kill_switch:
            return False
        if self.config.max_trades_per_day and self._daily_trade_count >= self.config.max_trades_per_day:
            return False
        if self.config.max_daily_loss and self._daily_pnl <= -abs(self.config.max_daily_loss):
            self._kill_switch = True
            return False
        return True

    def trigger_kill_switch(self):
        self._kill_switch = True

    def reset_daily_counters(self):
        self._daily_trade_count = 0
        self._daily_pnl = 0.0
        self._kill_switch = False

    def record_trade_result(self, pnl: float):
        self._daily_trade_count += 1
        self._daily_pnl += pnl
        if self.config.max_daily_loss and self._daily_pnl <= -abs(self.config.max_daily_loss):
            self._kill_switch = True

    # -------------------------------------------------------------------------
    # Utility: compute take profit from entry + stop loss
    # -------------------------------------------------------------------------

    def compute_take_profit(self, entry: float, stop_loss: float, direction: str) -> float:
        risk = abs(entry - stop_loss)
        reward = risk * self.config.risk_reward_ratio
        if direction == "long":
            return entry + reward
        return entry - reward
