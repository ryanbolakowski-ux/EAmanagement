"""
Example: ICT Liquidity Sweep + FVG Strategy
This implements the core strategy described in the project spec:
  1. Detect liquidity sweep (previous high/low taken)
  2. Identify Fair Value Gap (FVG) post-sweep
  3. Wait for price to return into FVG
  4. Drop to lower timeframe, confirm inverse FVG (IFVG)
  5. Execute trade with SL/TP based on RR

This serves as a template users can clone and customise.
"""
from typing import Optional
import pandas as pd

from .base_strategy import BaseStrategy, StrategyConfig, TradeSignal, SignalType
from .indicators import (
    detect_liquidity_sweeps,
    detect_fvgs,
    detect_inverse_fvgs,
    price_in_fvg,
    is_in_session,
    get_tick_size,
)


class LiquiditySweepFVGStrategy(BaseStrategy):
    """
    ICT-based Liquidity Sweep + FVG strategy.
    Primary TF: 15m for sweep + FVG detection
    Execution TF: 1m for IFVG confirmation
    """

    def on_bar(self, bars: dict[str, pd.DataFrame]) -> Optional[TradeSignal]:
        primary_tf = self.config.primary_timeframe
        exec_tf = self.config.execution_timeframe

        if primary_tf not in bars or exec_tf not in bars:
            return None

        df_primary = bars[primary_tf]
        df_exec    = bars[exec_tf]

        if len(df_primary) < 20 or len(df_exec) < 5:
            return None

        # 1. Session filter
        latest_ts = df_primary.index[-1] if isinstance(df_primary.index, pd.DatetimeIndex) else df_primary["timestamp"].iloc[-1]
        if not is_in_session(latest_ts, self.config.session_filters):
            return None

        # 2. Risk controls
        if not self.check_risk_controls():
            return None

        instrument = self.config.instruments[0]
        tick_size  = get_tick_size(instrument)

        # 3. Detect sweeps on primary timeframe
        sweeps = detect_liquidity_sweeps(df_primary, instrument=instrument)
        if not sweeps:
            return None

        latest_sweep = sweeps[-1]
        if latest_sweep.sweep_bar_index < len(df_primary) - 3:
            return None  # Sweep not recent

        # 4. Detect FVGs after the sweep
        post_sweep_df = df_primary.iloc[latest_sweep.sweep_bar_index - 2:]
        fvgs = detect_fvgs(post_sweep_df, instrument=instrument, min_size_ticks=self.config.fvg_min_size_ticks)

        if not fvgs:
            return None

        # Filter FVGs by sweep direction
        if latest_sweep.direction == "low_sweep":
            relevant_fvgs = [f for f in fvgs if f.direction == "bullish"]
        else:
            relevant_fvgs = [f for f in fvgs if f.direction == "bearish"]

        if not relevant_fvgs:
            return None

        target_fvg = relevant_fvgs[-1]

        # 5. Check if current price is inside the FVG (price returned to it)
        current_price = float(df_exec["close"].iloc[-1])
        if not price_in_fvg(current_price, target_fvg):
            return None

        # 6. Confirm IFVG on execution timeframe
        exec_fvgs = detect_fvgs(df_exec.iloc[-20:], instrument=instrument, min_size_ticks=2)
        ifvgs = detect_inverse_fvgs(exec_fvgs, df_exec.iloc[-20:], instrument=instrument)

        if not ifvgs:
            return None

        # 7. Build trade signal
        if latest_sweep.direction == "low_sweep":
            signal = SignalType.LONG
            if self.config.stop_loss_type == "ticks" and self.config.stop_loss_ticks:
                stop_loss = current_price - (self.config.stop_loss_ticks * tick_size)
            else:
                stop_loss = target_fvg.low - (2 * tick_size)  # Below FVG
        else:
            signal = SignalType.SHORT
            if self.config.stop_loss_type == "ticks" and self.config.stop_loss_ticks:
                stop_loss = current_price + (self.config.stop_loss_ticks * tick_size)
            else:
                stop_loss = target_fvg.high + (2 * tick_size)  # Above FVG

        take_profit = self.compute_take_profit(current_price, stop_loss, signal.value)

        return TradeSignal(
            signal=signal,
            instrument=instrument,
            entry_price=current_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            contracts=self.config.max_contracts,
            metadata={
                "sweep_direction": latest_sweep.direction,
                "swept_level": latest_sweep.swept_level,
                "fvg_high": target_fvg.high,
                "fvg_low": target_fvg.low,
                "fvg_size_ticks": target_fvg.size_ticks,
                "ifvg_confirmed": True,
            },
        )

    def on_tick(self, tick: dict) -> Optional[TradeSignal]:
        # For tick-level execution, defer to on_bar logic
        # This method is used for exit management in live/paper trading
        return None
