"""
Market structure indicators used by the strategy engine.
Implements: Liquidity Sweeps, Fair Value Gaps (FVG), Inverse FVGs (IFVG).
All functions operate on pandas DataFrames with OHLCV columns.
"""
from dataclasses import dataclass
from typing import Optional
import pandas as pd
import numpy as np


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FairValueGap:
    direction: str          # "bullish" or "bearish"
    high: float
    low: float
    midpoint: float
    bar_index: int
    timestamp: pd.Timestamp
    size_ticks: float
    filled: bool = False

    @property
    def size(self) -> float:
        return self.high - self.low


@dataclass
class LiquiditySweep:
    direction: str          # "high_sweep" or "low_sweep"
    swept_level: float
    sweep_bar_index: int
    sweep_timestamp: pd.Timestamp
    sweep_high: float
    sweep_low: float
    sweep_close: float


@dataclass
class SwingLevel:
    direction: str          # "high" or "low"
    price: float
    bar_index: int
    timestamp: pd.Timestamp


# ─────────────────────────────────────────────────────────────────────────────
# Instrument tick sizes
# ─────────────────────────────────────────────────────────────────────────────

TICK_SIZES = {
    "ES": 0.25,
    "NQ": 0.25,
    "RTY": 0.10,
    "YM": 1.0,
    "CL": 0.01,
    "GC": 0.10,
}

def get_tick_size(instrument: str) -> float:
    return TICK_SIZES.get(instrument.upper(), 0.25)


def price_to_ticks(price_diff: float, instrument: str) -> float:
    tick = get_tick_size(instrument)
    return abs(price_diff) / tick


# ─────────────────────────────────────────────────────────────────────────────
# Swing High / Swing Low detection
# ─────────────────────────────────────────────────────────────────────────────

def find_swing_highs(df: pd.DataFrame, lookback: int = 5) -> list[SwingLevel]:
    """Find swing highs: bars where high is the highest in +/- lookback bars."""
    swings = []
    highs = df["high"].values
    for i in range(lookback, len(highs) - lookback):
        window = highs[i - lookback: i + lookback + 1]
        if highs[i] == window.max():
            swings.append(SwingLevel(
                direction="high",
                price=float(highs[i]),
                bar_index=i,
                timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else df["timestamp"].iloc[i],
            ))
    return swings


def find_swing_lows(df: pd.DataFrame, lookback: int = 5) -> list[SwingLevel]:
    """Find swing lows: bars where low is the lowest in +/- lookback bars."""
    swings = []
    lows = df["low"].values
    for i in range(lookback, len(lows) - lookback):
        window = lows[i - lookback: i + lookback + 1]
        if lows[i] == window.min():
            swings.append(SwingLevel(
                direction="low",
                price=float(lows[i]),
                bar_index=i,
                timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else df["timestamp"].iloc[i],
            ))
    return swings


# ─────────────────────────────────────────────────────────────────────────────
# Liquidity Sweep Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_liquidity_sweeps(
    df: pd.DataFrame,
    lookback: int = 5,
    instrument: str = "ES",
    min_sweep_ticks: float = 1.0,
) -> list[LiquiditySweep]:
    """
    Detect when price sweeps a previous swing high or low and closes back inside.
    A sweep of a high = wick above prior swing high + close below it (bearish sweep).
    A sweep of a low  = wick below prior swing low  + close above it (bullish sweep).
    """
    sweeps = []
    tick = get_tick_size(instrument)

    swing_highs = find_swing_highs(df, lookback)
    swing_lows  = find_swing_lows(df, lookback)

    for i in range(lookback + 1, len(df)):
        bar = df.iloc[i]

        # Check against recent swing highs (bearish sweep)
        for sh in swing_highs:
            if sh.bar_index >= i:
                continue
            if i - sh.bar_index > lookback * 3:
                continue  # Too old
            swept_amount = (bar["high"] - sh.price) / tick
            if swept_amount >= min_sweep_ticks and bar["close"] < sh.price:
                sweeps.append(LiquiditySweep(
                    direction="high_sweep",
                    swept_level=sh.price,
                    sweep_bar_index=i,
                    sweep_timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else df["timestamp"].iloc[i],
                    sweep_high=float(bar["high"]),
                    sweep_low=float(bar["low"]),
                    sweep_close=float(bar["close"]),
                ))

        # Check against recent swing lows (bullish sweep)
        for sl in swing_lows:
            if sl.bar_index >= i:
                continue
            if i - sl.bar_index > lookback * 3:
                continue
            swept_amount = (sl.price - bar["low"]) / tick
            if swept_amount >= min_sweep_ticks and bar["close"] > sl.price:
                sweeps.append(LiquiditySweep(
                    direction="low_sweep",
                    swept_level=sl.price,
                    sweep_bar_index=i,
                    sweep_timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else df["timestamp"].iloc[i],
                    sweep_high=float(bar["high"]),
                    sweep_low=float(bar["low"]),
                    sweep_close=float(bar["close"]),
                ))

    return sweeps


# ─────────────────────────────────────────────────────────────────────────────
# Fair Value Gap (FVG) Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_fvgs(
    df: pd.DataFrame,
    instrument: str = "ES",
    min_size_ticks: float = 4.0,
    max_size_ticks: Optional[float] = None,
) -> list[FairValueGap]:
    """
    FVG = 3-bar pattern:
      Bullish FVG: bar[i-2].high < bar[i].low  (gap between candle 1 and candle 3)
      Bearish FVG: bar[i-2].low  > bar[i].high
    """
    fvgs = []
    tick = get_tick_size(instrument)

    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c3 = df.iloc[i]
        ts = df.index[i] if isinstance(df.index, pd.DatetimeIndex) else df["timestamp"].iloc[i]

        # Bullish FVG
        if c1["high"] < c3["low"]:
            gap_low  = float(c1["high"])
            gap_high = float(c3["low"])
            size_ticks = price_to_ticks(gap_high - gap_low, instrument)
            if size_ticks >= min_size_ticks and (max_size_ticks is None or size_ticks <= max_size_ticks):
                fvgs.append(FairValueGap(
                    direction="bullish",
                    high=gap_high,
                    low=gap_low,
                    midpoint=(gap_high + gap_low) / 2,
                    bar_index=i,
                    timestamp=ts,
                    size_ticks=size_ticks,
                ))

        # Bearish FVG
        elif c1["low"] > c3["high"]:
            gap_low  = float(c3["high"])
            gap_high = float(c1["low"])
            size_ticks = price_to_ticks(gap_high - gap_low, instrument)
            if size_ticks >= min_size_ticks and (max_size_ticks is None or size_ticks <= max_size_ticks):
                fvgs.append(FairValueGap(
                    direction="bearish",
                    high=gap_high,
                    low=gap_low,
                    midpoint=(gap_high + gap_low) / 2,
                    bar_index=i,
                    timestamp=ts,
                    size_ticks=size_ticks,
                ))

    return fvgs


def mark_filled_fvgs(fvgs: list[FairValueGap], df: pd.DataFrame) -> list[FairValueGap]:
    """Mark FVGs as filled once price closes fully through them."""
    for fvg in fvgs:
        subsequent = df.iloc[fvg.bar_index + 1:]
        if fvg.direction == "bullish":
            if (subsequent["low"] < fvg.low).any():
                fvg.filled = True
        else:
            if (subsequent["high"] > fvg.high).any():
                fvg.filled = True
    return fvgs


def price_in_fvg(price: float, fvg: FairValueGap) -> bool:
    """Returns True if price is inside the FVG zone."""
    return fvg.low <= price <= fvg.high


# ─────────────────────────────────────────────────────────────────────────────
# Inverse FVG (IFVG) Detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_inverse_fvgs(
    fvgs: list[FairValueGap],
    df: pd.DataFrame,
    instrument: str = "ES",
) -> list[FairValueGap]:
    """
    An IFVG forms when price returns to a previously detected FVG
    and then fails to continue — the zone flips from support to resistance or vice versa.
    Returns FVG objects with direction inverted (they now act in the opposite role).
    """
    ifvgs = []
    for fvg in fvgs:
        if fvg.filled:
            continue
        subsequent = df.iloc[fvg.bar_index + 1:]
        if fvg.direction == "bullish":
            # Price returns into the bullish FVG from above
            entered = subsequent[(subsequent["low"] <= fvg.high) & (subsequent["low"] >= fvg.low)]
            if not entered.empty:
                # Check if the candle that re-entered rejected (closed back above)
                entry_bar = entered.iloc[0]
                if entry_bar["close"] > fvg.midpoint:
                    # Rejection — it's now an IFVG (acts as support confirmed)
                    ifvgs.append(FairValueGap(
                        direction="bullish_confirmed",
                        high=fvg.high,
                        low=fvg.low,
                        midpoint=fvg.midpoint,
                        bar_index=fvg.bar_index,
                        timestamp=fvg.timestamp,
                        size_ticks=fvg.size_ticks,
                    ))
        elif fvg.direction == "bearish":
            entered = subsequent[(subsequent["high"] >= fvg.low) & (subsequent["high"] <= fvg.high)]
            if not entered.empty:
                entry_bar = entered.iloc[0]
                if entry_bar["close"] < fvg.midpoint:
                    ifvgs.append(FairValueGap(
                        direction="bearish_confirmed",
                        high=fvg.high,
                        low=fvg.low,
                        midpoint=fvg.midpoint,
                        bar_index=fvg.bar_index,
                        timestamp=fvg.timestamp,
                        size_ticks=fvg.size_ticks,
                    ))
    return ifvgs


# ─────────────────────────────────────────────────────────────────────────────
# Session Filter
# ─────────────────────────────────────────────────────────────────────────────

SESSION_HOURS_UTC = {
    "NY":     (13, 21),  # 9am–5pm EST
    "LONDON": (8, 16),
    "ASIA":   (0, 8),
    "NY_AM":  (13, 17),  # 9am–1pm EST only
}

def is_in_session(timestamp: pd.Timestamp, sessions: list[str]) -> bool:
    if not sessions:
        return True
    hour_utc = timestamp.hour
    for session in sessions:
        bounds = SESSION_HOURS_UTC.get(session.upper())
        if bounds and bounds[0] <= hour_utc < bounds[1]:
            return True
    return False
