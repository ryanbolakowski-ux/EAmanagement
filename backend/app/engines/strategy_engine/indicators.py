"""
Market structure indicators used by the strategy engine.
Implements: Liquidity Sweeps, Fair Value Gaps (FVG), Swing Detection.
Based on LuxAlgo FVG logic for gap detection and mitigation tracking.
"""
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
import numpy as np


@dataclass
class FairValueGap:
    direction: str
    high: float
    low: float
    midpoint: float
    bar_index: int
    timestamp: pd.Timestamp
    size_ticks: float
    filled: bool = False
    ce_level: float = 0.0  # Consequent Encroachment (50%)

    @property
    def size(self) -> float:
        return self.high - self.low


@dataclass
class LiquiditySweep:
    direction: str
    swept_level: float
    sweep_bar_index: int
    sweep_timestamp: pd.Timestamp
    sweep_high: float
    sweep_low: float
    sweep_close: float


@dataclass
class SwingLevel:
    direction: str
    price: float
    bar_index: int
    timestamp: pd.Timestamp


TICK_SIZES = {
    "ES": 0.25, "NQ": 0.25, "RTY": 0.10, "YM": 1.0, "CL": 0.01, "GC": 0.10,
}

def get_tick_size(instrument: str, use_etf: bool = False) -> float:
    if use_etf:
        # ETF equivalents have much smaller tick sizes
        ETF_TICK_SIZES = {"ES": 0.03, "NQ": 0.03, "RTY": 0.01, "YM": 0.05}
        return ETF_TICK_SIZES.get(instrument.upper(), 0.03)
    return TICK_SIZES.get(instrument.upper(), 0.25)

def price_to_ticks(price_diff: float, instrument: str) -> float:
    tick = get_tick_size(instrument)
    return abs(price_diff) / tick if tick > 0 else 0


def find_swing_highs(df: pd.DataFrame, lookback: int = 3) -> list[SwingLevel]:
    swings = []
    highs = df["high"].values
    for i in range(lookback, len(highs) - lookback):
        window = highs[i - lookback: i + lookback + 1]
        if highs[i] == window.max() and np.sum(window == highs[i]) == 1:
            swings.append(SwingLevel(
                direction="high", price=float(highs[i]), bar_index=i,
                timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now(),
            ))
    return swings


def find_swing_lows(df: pd.DataFrame, lookback: int = 3) -> list[SwingLevel]:
    swings = []
    lows = df["low"].values
    for i in range(lookback, len(lows) - lookback):
        window = lows[i - lookback: i + lookback + 1]
        if lows[i] == window.min() and np.sum(window == lows[i]) == 1:
            swings.append(SwingLevel(
                direction="low", price=float(lows[i]), bar_index=i,
                timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now(),
            ))
    return swings


def detect_liquidity_sweeps(
    df: pd.DataFrame, lookback: int = 3, instrument: str = "ES", min_sweep_ticks: float = 0.5,
) -> list[LiquiditySweep]:
    sweeps = []
    tick = get_tick_size(instrument)
    swing_highs = find_swing_highs(df, lookback)
    swing_lows = find_swing_lows(df, lookback)

    for i in range(lookback + 1, len(df)):
        bar = df.iloc[i]
        for sh in swing_highs:
            if sh.bar_index >= i or i - sh.bar_index > lookback * 4:
                continue
            swept_amount = (bar["high"] - sh.price) / tick
            if swept_amount >= min_sweep_ticks and bar["close"] < sh.price:
                sweeps.append(LiquiditySweep(
                    direction="high_sweep", swept_level=sh.price, sweep_bar_index=i,
                    sweep_timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now(),
                    sweep_high=float(bar["high"]), sweep_low=float(bar["low"]), sweep_close=float(bar["close"]),
                ))
        for sl in swing_lows:
            if sl.bar_index >= i or i - sl.bar_index > lookback * 4:
                continue
            swept_amount = (sl.price - bar["low"]) / tick
            if swept_amount >= min_sweep_ticks and bar["close"] > sl.price:
                sweeps.append(LiquiditySweep(
                    direction="low_sweep", swept_level=sl.price, sweep_bar_index=i,
                    sweep_timestamp=df.index[i] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now(),
                    sweep_high=float(bar["high"]), sweep_low=float(bar["low"]), sweep_close=float(bar["close"]),
                ))
    return sweeps


def detect_fvgs(
    df: pd.DataFrame, instrument: str = "ES",
    min_size_ticks: float = 1.0, max_size_ticks: Optional[float] = None,
    use_atr_filter: bool = False, atr_multiplier: float = 0.25,
) -> list[FairValueGap]:
    """
    LuxAlgo-style FVG detection.
    Bullish FVG: candle[i].low > candle[i-2].high (gap between wick of c1 and wick of c3)
    Bearish FVG: candle[i].high < candle[i-2].low
    Tracks CE (Consequent Encroachment = 50% of gap) and mitigation.
    """
    fvgs = []
    tick = get_tick_size(instrument)

    atr_values = None
    if use_atr_filter and len(df) >= 14:
        highs = df["high"].values
        lows = df["low"].values
        closes = df["close"].values
        tr = np.maximum(highs[1:] - lows[1:], np.maximum(
            np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])
        ))
        atr_values = pd.Series(tr).rolling(14).mean().values

    for i in range(2, len(df)):
        c1 = df.iloc[i - 2]
        c2 = df.iloc[i - 1]  # Middle candle (the displacement)
        c3 = df.iloc[i]
        ts = df.index[i] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now()

        # Bullish FVG: gap up - c3 low is above c1 high
        if c3["low"] > c1["high"]:
            gap_low = float(c1["high"])
            gap_high = float(c3["low"])
            gap_size = gap_high - gap_low
            size_ticks = gap_size / tick if tick > 0 else 0

            # ATR filter: gap must be significant relative to ATR
            if use_atr_filter and atr_values is not None and i - 1 < len(atr_values):
                atr = atr_values[i - 1]
                if atr > 0 and gap_size < atr * atr_multiplier:
                    continue

            if size_ticks >= min_size_ticks and (max_size_ticks is None or size_ticks <= max_size_ticks):
                ce = gap_low + gap_size * 0.5
                # Check if already filled by subsequent bars
                filled = False
                for j in range(i + 1, min(i + 20, len(df))):
                    if df.iloc[j]["low"] <= gap_low:
                        filled = True
                        break
                fvgs.append(FairValueGap(
                    direction="bullish", high=gap_high, low=gap_low,
                    midpoint=ce, bar_index=i, timestamp=ts,
                    size_ticks=size_ticks, filled=filled, ce_level=ce,
                ))

        # Bearish FVG: gap down - c3 high is below c1 low
        if c3["high"] < c1["low"]:
            gap_high = float(c1["low"])
            gap_low = float(c3["high"])
            gap_size = gap_high - gap_low
            size_ticks = gap_size / tick if tick > 0 else 0

            if use_atr_filter and atr_values is not None and i - 1 < len(atr_values):
                atr = atr_values[i - 1]
                if atr > 0 and gap_size < atr * atr_multiplier:
                    continue

            if size_ticks >= min_size_ticks and (max_size_ticks is None or size_ticks <= max_size_ticks):
                ce = gap_high - gap_size * 0.5
                filled = False
                for j in range(i + 1, min(i + 20, len(df))):
                    if df.iloc[j]["high"] >= gap_high:
                        filled = True
                        break
                fvgs.append(FairValueGap(
                    direction="bearish", high=gap_high, low=gap_low,
                    midpoint=ce, bar_index=i, timestamp=ts,
                    size_ticks=size_ticks, filled=filled, ce_level=ce,
                ))

    return fvgs



def detect_ifvgs(
    df: pd.DataFrame, instrument: str = "ES",
    min_size_ticks: float = 1.0, max_size_ticks: Optional[float] = None,
) -> list[FairValueGap]:
    """
    Inverse FVG (IFVG) detection.
    An IFVG forms when price fills/trades through an existing FVG and creates
    a new gap in the OPPOSITE direction.
    
    Bullish IFVG: A bearish FVG gets filled, and on the fill candle sequence,
    a new bullish gap forms (support level).
    Bearish IFVG: A bullish FVG gets filled, and on the fill candle sequence,
    a new bearish gap forms (resistance level).
    """
    fvgs = detect_fvgs(df, instrument, min_size_ticks=0.5)
    ifvgs = []
    tick = get_tick_size(instrument)
    
    for fvg in fvgs:
        if not fvg.filled:
            continue
        # Find where the FVG was filled
        for i in range(fvg.bar_index + 1, min(fvg.bar_index + 30, len(df))):
            filled_here = False
            if fvg.direction == "bullish" and df.iloc[i]["low"] <= fvg.low:
                filled_here = True
            elif fvg.direction == "bearish" and df.iloc[i]["high"] >= fvg.high:
                filled_here = True
            
            if filled_here and i + 2 < len(df):
                # Check for inverse gap forming at the fill point
                c1 = df.iloc[i]
                c3 = df.iloc[i + 2]
                ts = df.index[i + 2] if isinstance(df.index, pd.DatetimeIndex) else pd.Timestamp.now()
                
                if fvg.direction == "bearish" and c3["low"] > c1["high"]:
                    # Bearish FVG filled -> Bullish IFVG forms (support)
                    gap_low = float(c1["high"])
                    gap_high = float(c3["low"])
                    size_ticks = (gap_high - gap_low) / tick if tick > 0 else 0
                    if size_ticks >= min_size_ticks:
                        ce = gap_low + (gap_high - gap_low) * 0.5
                        ifvgs.append(FairValueGap(
                            direction="bullish", high=gap_high, low=gap_low,
                            midpoint=ce, bar_index=i + 2, timestamp=ts,
                            size_ticks=size_ticks, filled=False, ce_level=ce,
                        ))
                
                elif fvg.direction == "bullish" and c3["high"] < c1["low"]:
                    # Bullish FVG filled -> Bearish IFVG forms (resistance)
                    gap_high = float(c1["low"])
                    gap_low = float(c3["high"])
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


def is_in_session(timestamp, session_filters: list[str]) -> bool:
    if not session_filters:
        return True
    if not isinstance(timestamp, pd.Timestamp):
        timestamp = pd.Timestamp(timestamp)
    if timestamp.tz is not None:
        est = timestamp.tz_convert("US/Eastern")
    else:
        est = timestamp.tz_localize("UTC").tz_convert("US/Eastern")
    hour = est.hour
    minute = est.minute
    t = hour * 60 + minute

    sessions = {
        "NY": (9 * 60 + 30, 16 * 60),
        "NY_AM": (9 * 60 + 30, 11 * 60),
        "NY_PM": (13 * 60 + 30, 16 * 60 + 30),
        "LONDON": (2 * 60, 5 * 60),
        "LONDON_CLOSE": (10 * 60, 12 * 60),
        "ASIA": (20 * 60, 24 * 60),
    }

    for sf in session_filters:
        key = sf.upper()
        if key in sessions:
            start, end = sessions[key]
            if start <= t < end:
                return True
    return False


def compute_rsi(closes, period: int = 14):
    """Wilder's RSI on the most recent bar. Returns None if not enough data."""
    s = pd.Series(closes)
    if len(s) < period + 1:
        return None
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    last_loss = avg_loss.iloc[-1]
    if last_loss == 0:
        return 100.0
    rs = avg_gain.iloc[-1] / last_loss
    return float(100.0 - (100.0 / (1.0 + rs)))


def compute_session_vwap(df):
    """Session-anchored VWAP for the latest bar. Resets at the start of each
    UTC trading day. Returns None if df is empty.
    """
    if df is None or df.empty:
        return None
    if not isinstance(df.index, pd.DatetimeIndex):
        return None
    last_ts = df.index[-1]
    anchor = pd.Timestamp(last_ts.year, last_ts.month, last_ts.day, tz=last_ts.tz)
    today = df[df.index >= anchor]
    if today.empty:
        today = df.tail(60)
    typical = (today["high"] + today["low"] + today["close"]) / 3.0
    vol = today["volume"].clip(lower=0).fillna(0) if "volume" in today else pd.Series([0] * len(today), index=today.index)
    total_vol = vol.sum()
    if total_vol <= 0:
        return float(typical.iloc[-1])
    return float((typical * vol).sum() / total_vol)
