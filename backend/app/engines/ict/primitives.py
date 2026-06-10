"""ICT primitives - the shared vocabulary every setup composes.

This module (a) re-exports the existing, battle-tested primitives from
``strategy_engine/indicators.py`` so setups have one import surface, and
(b) adds the NEW pure primitives the proposal (SS1) flagged as missing:
``detect_mss`` (Market Structure Shift / reversal), ``detect_bos`` (Break of
Structure / continuation), ``session_range`` (ET-window high/low),
``ote_levels`` (Optimal Trade Entry fibs), plus a small ``price_in_fvg``
helper and new killzone session keys.

Everything here is a PURE function (no I/O, no engine state) and is NOT yet
wired into any strategy - so it carries zero behavior risk. Timestamps are
reasoned about in America/New_York via ``zoneinfo``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

# ── Re-export existing primitives (one import surface for setups) ──
from app.engines.strategy_engine.indicators import (  # noqa: F401
    detect_liquidity_sweeps,
    detect_fvgs,
    detect_ifvgs,
    find_swing_highs,
    find_swing_lows,
    is_in_session,
    get_tick_size,
    FairValueGap,
    LiquiditySweep,
    SwingLevel,
)

ET = ZoneInfo("America/New_York")

# ── New ICT killzone windows (ET, minutes-since-midnight), additive ──
# These EXTEND (never replace) the indicators.is_in_session table; that
# function is patched in-place to merge these keys. Kept here too so setups
# and tests have a single source for the new windows.
ICT_SESSIONS: dict[str, tuple[int, int]] = {
    "SILVER_BULLET": (10 * 60, 11 * 60),      # 10:00-11:00 ET
    "NY_OPEN": (9 * 60 + 30, 10 * 60),         # 09:30-10:00 ET
    "NY_PM_ICT": (13 * 60 + 30, 16 * 60),      # 13:30-16:00 ET
    "LONDON_OPEN": (2 * 60, 3 * 60),           # 02:00-03:00 ET
}

__all__ = [
    # re-exports
    "detect_liquidity_sweeps", "detect_fvgs", "detect_ifvgs",
    "find_swing_highs", "find_swing_lows", "is_in_session",
    "get_tick_size", "FairValueGap", "LiquiditySweep", "SwingLevel",
    # new
    "MSS", "BOS", "detect_mss", "detect_bos", "session_range",
    "ote_levels", "price_in_fvg", "ICT_SESSIONS",
]


# ---------------------------------------------------------------------------
# price_in_fvg - small helper (indicators.py has no standalone version).
# ---------------------------------------------------------------------------
def price_in_fvg(price: float, fvg: FairValueGap, inclusive: bool = True) -> bool:
    """True iff ``price`` falls within the FVG's [low, high] band."""
    if fvg is None:
        return False
    lo, hi = float(fvg.low), float(fvg.high)
    if lo > hi:
        lo, hi = hi, lo
    if inclusive:
        return lo <= float(price) <= hi
    return lo < float(price) < hi


# ---------------------------------------------------------------------------
# Market-structure breaks: MSS (reversal) and BOS (continuation).
# ---------------------------------------------------------------------------
@dataclass
class MSS:
    """Market Structure Shift - a reversal confirmation.

    direction: "up" (bullish reversal) | "down" (bearish reversal).
    broken_level: the counter-trend swing level price broke.
    bar_index: index of the bar that broke it (the displacement bar).
    prior_trend: the trend that was in force before the shift.
    """
    direction: str
    broken_level: float
    bar_index: int
    prior_trend: str


@dataclass
class BOS:
    """Break of Structure - a continuation confirmation.

    direction: "up" | "down" (same as the prevailing trend).
    broken_level: the with-trend swing level price broke.
    bar_index: index of the breaking bar.
    trend: the prevailing trend (== direction).
    """
    direction: str
    broken_level: float
    bar_index: int
    trend: str


def _recent_trend(highs: list[SwingLevel], lows: list[SwingLevel]) -> Optional[str]:
    """Classify the local trend from the last two swing highs and lows.

    Up   = higher-high AND higher-low. Down = lower-high AND lower-low.
    Returns "up" | "down" | None (ambiguous / insufficient swings).
    """
    if len(highs) < 2 or len(lows) < 2:
        # Fall back to a 1-swing comparison only when one side is thin: use
        # whichever pair we have. If neither has 2, we can't classify.
        if len(highs) >= 2:
            return "up" if highs[-1].price > highs[-2].price else "down"
        if len(lows) >= 2:
            return "up" if lows[-1].price > lows[-2].price else "down"
        return None
    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    lh = highs[-1].price < highs[-2].price
    ll = lows[-1].price < lows[-2].price
    if hh and hl:
        return "up"
    if lh and ll:
        return "down"
    # Mixed structure: lean on the most recent swing of either kind.
    last_swing = highs[-1] if highs[-1].bar_index >= lows[-1].bar_index else lows[-1]
    if last_swing.direction == "high":
        return "up" if hh else "down"
    return "up" if hl else "down"


def _is_displacement(df: pd.DataFrame, idx: int, body_pct: float, range_mult: float,
                     avg_window: int = 20) -> bool:
    """A single-bar displacement proxy: large body AND large range vs recent avg."""
    n = len(df)
    if idx < 0 or idx >= n:
        return False
    bar = df.iloc[idx]
    rng = float(bar["high"] - bar["low"])
    if rng <= 0:
        return False
    body = abs(float(bar["close"] - bar["open"]))
    lo = max(0, idx - avg_window)
    window = df.iloc[lo:idx + 1]
    avg_range = float((window["high"] - window["low"]).mean())
    if avg_range <= 0:
        return False
    return (body / rng) >= body_pct and (rng / avg_range) >= range_mult


def detect_mss(
    df: pd.DataFrame,
    lookback: int = 3,
    body_pct: float = 0.45,
    range_mult: float = 1.0,
) -> Optional[MSS]:
    """Detect a Market Structure Shift (reversal) on the latest bar.

    The MSS is the canonical post-sweep reversal trigger: price breaks the most
    recent **counter-trend** swing **with displacement**.

      * Prevailing UP trend  -> the counter-trend swing is the most recent swing
        LOW. A close BELOW it with displacement = bearish MSS ("down").
      * Prevailing DOWN trend -> counter-trend swing is the most recent swing
        HIGH. A close ABOVE it with displacement = bullish MSS ("up").

    Returns an :class:`MSS` (direction, broken level, breaking bar index, prior
    trend) or ``None`` if no MSS is confirmed on the final bar.
    """
    if df is None or len(df) < lookback * 2 + 2:
        return None
    highs = find_swing_highs(df, lookback)
    lows = find_swing_lows(df, lookback)
    trend = _recent_trend(highs, lows)
    if trend is None:
        return None

    last_idx = len(df) - 1
    last = df.iloc[last_idx]
    last_close = float(last["close"])

    if trend == "up":
        # counter-trend swing = most recent swing low strictly before last bar
        prior = [s for s in lows if s.bar_index < last_idx]
        if not prior:
            return None
        swing = prior[-1]
        if last_close < swing.price and _is_displacement(df, last_idx, body_pct, range_mult):
            return MSS(direction="down", broken_level=float(swing.price),
                       bar_index=last_idx, prior_trend="up")
    else:  # trend == "down"
        prior = [s for s in highs if s.bar_index < last_idx]
        if not prior:
            return None
        swing = prior[-1]
        if last_close > swing.price and _is_displacement(df, last_idx, body_pct, range_mult):
            return MSS(direction="up", broken_level=float(swing.price),
                       bar_index=last_idx, prior_trend="down")
    return None


def detect_bos(
    df: pd.DataFrame,
    lookback: int = 3,
    body_pct: float = 0.0,
    range_mult: float = 0.0,
) -> Optional[BOS]:
    """Detect a Break of Structure (continuation) on the latest bar.

    BOS = price breaks the most recent **with-trend** swing, confirming the
    trend continues:

      * UP trend  -> a close ABOVE the most recent swing HIGH = "up" BOS.
      * DOWN trend -> a close BELOW the most recent swing LOW  = "down" BOS.

    Displacement is OPTIONAL for BOS (defaults off: a clean structural break is
    enough); pass ``body_pct``/``range_mult`` > 0 to require it.
    Returns a :class:`BOS` or ``None``.
    """
    if df is None or len(df) < lookback * 2 + 2:
        return None
    highs = find_swing_highs(df, lookback)
    lows = find_swing_lows(df, lookback)
    trend = _recent_trend(highs, lows)
    if trend is None:
        return None

    last_idx = len(df) - 1
    last_close = float(df.iloc[last_idx]["close"])
    need_disp = body_pct > 0 or range_mult > 0

    if trend == "up":
        prior = [s for s in highs if s.bar_index < last_idx]
        if not prior:
            return None
        swing = prior[-1]
        if last_close > swing.price and (
            not need_disp or _is_displacement(df, last_idx, body_pct, range_mult)
        ):
            return BOS(direction="up", broken_level=float(swing.price),
                       bar_index=last_idx, trend="up")
    else:
        prior = [s for s in lows if s.bar_index < last_idx]
        if not prior:
            return None
        swing = prior[-1]
        if last_close < swing.price and (
            not need_disp or _is_displacement(df, last_idx, body_pct, range_mult)
        ):
            return BOS(direction="down", broken_level=float(swing.price),
                       bar_index=last_idx, trend="down")
    return None


# ---------------------------------------------------------------------------
# session_range - high/low of bars inside an ET time window.
# ---------------------------------------------------------------------------
def _coerce_et_time(value) -> dtime:
    """Accept "HH:MM", datetime.time, or (h, m) and return a datetime.time."""
    if isinstance(value, dtime):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return dtime(int(value[0]), int(value[1]))
    if isinstance(value, str):
        parts = value.strip().split(":")
        return dtime(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    raise TypeError(f"unsupported ET time value: {value!r}")


def session_range(df: pd.DataFrame, start_et, end_et) -> tuple[Optional[float], Optional[float]]:
    """Return ``(high, low)`` of all bars whose ET wall-clock time is within
    ``[start_et, end_et)``.

    Times are compared in America/New_York. ``start_et``/``end_et`` may be
    "HH:MM" strings, ``datetime.time``, or ``(hour, minute)`` tuples. A window
    that wraps midnight (e.g. 20:00->02:00) is supported. Returns
    ``(None, None)`` when no bars fall in the window or the frame is empty/
    non-datetime-indexed - so callers can guard cheaply.
    """
    if df is None or len(df) == 0 or not isinstance(df.index, pd.DatetimeIndex):
        return None, None
    start = _coerce_et_time(start_et)
    end = _coerce_et_time(end_et)

    idx = df.index
    if idx.tz is None:
        idx_et = idx.tz_localize("UTC").tz_convert(ET)
    else:
        idx_et = idx.tz_convert(ET)

    minutes = idx_et.hour * 60 + idx_et.minute
    s = start.hour * 60 + start.minute
    e = end.hour * 60 + end.minute

    if s <= e:
        mask = (minutes >= s) & (minutes < e)
    else:  # wraps midnight
        mask = (minutes >= s) | (minutes < e)

    sub = df[np.asarray(mask)]
    if len(sub) == 0:
        return None, None
    return float(sub["high"].max()), float(sub["low"].min())


# ---------------------------------------------------------------------------
# ote_levels - Optimal Trade Entry fib levels of the impulse leg.
# ---------------------------------------------------------------------------
def ote_levels(swing_high: float, swing_low: float, direction: str) -> dict:
    """Optimal Trade Entry fib retracement levels of an impulse leg.

    The OTE zone is the 0.62-0.79 retracement, with **0.705** the focal entry.

    For a **bullish** (long) setup the impulse is low->high and we retrace DOWN
    from the high, so a higher fib fraction sits at a LOWER price. For a
    **bearish** (short) setup the impulse is high->low and we retrace UP from
    the low.

    Returns a dict with keys ``0.62``, ``0.705``, ``0.79`` (the prices),
    plus ``"entry"`` (== 0.705), ``"high"``, ``"low"``, ``"direction"``.
    """
    hi = float(max(swing_high, swing_low))
    lo = float(min(swing_high, swing_low))
    leg = hi - lo
    d = (direction or "").lower()

    def _lvl(frac: float) -> float:
        if d in ("long", "bullish", "up"):
            # retrace down from the high
            return hi - leg * frac
        # retrace up from the low
        return lo + leg * frac

    levels = {
        0.62: _lvl(0.62),
        0.705: _lvl(0.705),
        0.79: _lvl(0.79),
    }
    levels["entry"] = levels[0.705]
    levels["high"] = hi
    levels["low"] = lo
    levels["direction"] = "long" if d in ("long", "bullish", "up") else "short"
    return levels
