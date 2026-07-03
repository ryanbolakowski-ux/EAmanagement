"""FAST-BT-V1: vectorized twins of the strategy_engine indicator scanners.

Each *_fast function is an exact-parity re-implementation of its namesake in
app/engines/strategy_engine/indicators.py: same control flow, same float
arithmetic on the same np.float64 values, same append order — the ONLY change
is that per-row access goes through numpy arrays pulled out of the DataFrame
once, instead of a pandas .iloc row materialization per bar (which dominated
the backtest profile: detect_fvgs alone was 8.2s of a 26s two-week run).

These are used ONLY when a strategy runs under BacktestRunner with
V2_FAST_BACKTEST != "0" (see ict_strategy._fast_backtest). Live/paper trading
never sets that flag, so they keep calling the original indicators module.

Do not "improve" the logic here — any semantic drift from indicators.py is a
parity bug (guarded by tests/test_fast_backtest_parity.py).
"""
from typing import Optional
import numpy as np
import pandas as pd

from app.engines.strategy_engine.indicators import (
    FairValueGap, LiquiditySweep, get_tick_size,
    find_swing_highs, find_swing_lows,
)


def detect_fvgs_fast(
    df: pd.DataFrame, instrument: str = "ES",
    min_size_ticks: float = 1.0, max_size_ticks: Optional[float] = None,
    use_atr_filter: bool = False, atr_multiplier: float = 0.25,
) -> list[FairValueGap]:
    """Exact-parity twin of indicators.detect_fvgs (numpy row access)."""
    fvgs: list[FairValueGap] = []
    tick = get_tick_size(instrument)
    n = len(df)
    if n < 3:
        return fvgs

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()

    atr_values = None
    if use_atr_filter and n >= 14:
        closes = df["close"].to_numpy()
        tr = np.maximum(highs[1:] - lows[1:], np.maximum(
            np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])
        ))
        atr_values = pd.Series(tr).rolling(14).mean().values

    is_dt_index = isinstance(df.index, pd.DatetimeIndex)
    index = df.index

    for i in range(2, n):
        c1_high = highs[i - 2]
        c1_low = lows[i - 2]
        c3_high = highs[i]
        c3_low = lows[i]
        ts = index[i] if is_dt_index else pd.Timestamp.now()

        # Bullish FVG: gap up - c3 low is above c1 high
        if c3_low > c1_high:
            gap_low = float(c1_high)
            gap_high = float(c3_low)
            gap_size = gap_high - gap_low
            size_ticks = gap_size / tick if tick > 0 else 0

            if use_atr_filter and atr_values is not None and i - 1 < len(atr_values):
                atr = atr_values[i - 1]
                if atr > 0 and gap_size < atr * atr_multiplier:
                    continue  # NB: skips the bearish check for this i too (parity)

            if size_ticks >= min_size_ticks and (max_size_ticks is None or size_ticks <= max_size_ticks):
                ce = gap_low + gap_size * 0.5
                filled = False
                for j in range(i + 1, min(i + 20, n)):
                    if lows[j] <= gap_low:
                        filled = True
                        break
                fvgs.append(FairValueGap(
                    direction="bullish", high=gap_high, low=gap_low,
                    midpoint=ce, bar_index=i, timestamp=ts,
                    size_ticks=size_ticks, filled=filled, ce_level=ce,
                ))

        # Bearish FVG: gap down - c3 high is below c1 low
        if c3_high < c1_low:
            gap_high = float(c1_low)
            gap_low = float(c3_high)
            gap_size = gap_high - gap_low
            size_ticks = gap_size / tick if tick > 0 else 0

            if use_atr_filter and atr_values is not None and i - 1 < len(atr_values):
                atr = atr_values[i - 1]
                if atr > 0 and gap_size < atr * atr_multiplier:
                    continue

            if size_ticks >= min_size_ticks and (max_size_ticks is None or size_ticks <= max_size_ticks):
                ce = gap_high - gap_size * 0.5
                filled = False
                for j in range(i + 1, min(i + 20, n)):
                    if highs[j] >= gap_high:
                        filled = True
                        break
                fvgs.append(FairValueGap(
                    direction="bearish", high=gap_high, low=gap_low,
                    midpoint=ce, bar_index=i, timestamp=ts,
                    size_ticks=size_ticks, filled=filled, ce_level=ce,
                ))

    return fvgs


def detect_ifvgs_fast(
    df: pd.DataFrame, instrument: str = "ES",
    min_size_ticks: float = 1.0, max_size_ticks: Optional[float] = None,
) -> list[FairValueGap]:
    """Exact-parity twin of indicators.detect_ifvgs (numpy row access)."""
    fvgs = detect_fvgs_fast(df, instrument, min_size_ticks=0.5)
    ifvgs: list[FairValueGap] = []
    tick = get_tick_size(instrument)
    n = len(df)
    if n == 0:
        return ifvgs

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    is_dt_index = isinstance(df.index, pd.DatetimeIndex)
    index = df.index

    for fvg in fvgs:
        if not fvg.filled:
            continue
        # Find where the FVG was filled
        for i in range(fvg.bar_index + 1, min(fvg.bar_index + 30, n)):
            filled_here = False
            if fvg.direction == "bullish" and lows[i] <= fvg.low:
                filled_here = True
            elif fvg.direction == "bearish" and highs[i] >= fvg.high:
                filled_here = True

            if filled_here and i + 2 < n:
                c1_high = highs[i]
                c1_low = lows[i]
                c3_high = highs[i + 2]
                c3_low = lows[i + 2]
                ts = index[i + 2] if is_dt_index else pd.Timestamp.now()

                if fvg.direction == "bearish" and c3_low > c1_high:
                    gap_low = float(c1_high)
                    gap_high = float(c3_low)
                    size_ticks = (gap_high - gap_low) / tick if tick > 0 else 0
                    if size_ticks >= min_size_ticks:
                        ce = gap_low + (gap_high - gap_low) * 0.5
                        ifvgs.append(FairValueGap(
                            direction="bullish", high=gap_high, low=gap_low,
                            midpoint=ce, bar_index=i + 2, timestamp=ts,
                            size_ticks=size_ticks, filled=False, ce_level=ce,
                        ))

                elif fvg.direction == "bullish" and c3_high < c1_low:
                    gap_high = float(c1_low)
                    gap_low = float(c3_high)
                    size_ticks = (gap_high - gap_low) / tick if tick > 0 else 0
                    if size_ticks >= min_size_ticks:
                        ce = gap_high - (gap_high - gap_low) * 0.5
                        ifvgs.append(FairValueGap(
                            direction="bearish", high=gap_high, low=gap_low,
                            midpoint=ce, bar_index=i + 2, timestamp=ts,
                            size_ticks=size_ticks, filled=False, ce_level=ce,
                        ))
                break

    return ifvgs


def detect_liquidity_sweeps_fast(
    df: pd.DataFrame, lookback: int = 3, instrument: str = "ES", min_sweep_ticks: float = 0.5,
) -> list[LiquiditySweep]:
    """Exact-parity twin of indicators.detect_liquidity_sweeps (numpy rows)."""
    sweeps: list[LiquiditySweep] = []
    tick = get_tick_size(instrument)
    swing_highs = find_swing_highs(df, lookback)
    swing_lows = find_swing_lows(df, lookback)
    n = len(df)

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    is_dt_index = isinstance(df.index, pd.DatetimeIndex)
    index = df.index

    for i in range(lookback + 1, n):
        bar_high = highs[i]
        bar_low = lows[i]
        bar_close = closes[i]
        for sh in swing_highs:
            if sh.bar_index >= i or i - sh.bar_index > lookback * 4:
                continue
            swept_amount = (bar_high - sh.price) / tick
            if swept_amount >= min_sweep_ticks and bar_close < sh.price:
                sweeps.append(LiquiditySweep(
                    direction="high_sweep", swept_level=sh.price, sweep_bar_index=i,
                    sweep_timestamp=index[i] if is_dt_index else pd.Timestamp.now(),
                    sweep_high=float(bar_high), sweep_low=float(bar_low), sweep_close=float(bar_close),
                ))
        for sl in swing_lows:
            if sl.bar_index >= i or i - sl.bar_index > lookback * 4:
                continue
            swept_amount = (sl.price - bar_low) / tick
            if swept_amount >= min_sweep_ticks and bar_close > sl.price:
                sweeps.append(LiquiditySweep(
                    direction="low_sweep", swept_level=sl.price, sweep_bar_index=i,
                    sweep_timestamp=index[i] if is_dt_index else pd.Timestamp.now(),
                    sweep_high=float(bar_high), sweep_low=float(bar_low), sweep_close=float(bar_close),
                ))
    return sweeps
