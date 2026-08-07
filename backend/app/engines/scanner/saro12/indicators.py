"""Saro 1.2 indicators — pure pandas/numpy, deterministic, unit-tested.

NO TA-Lib in the image. RSI/MACD use the exact ewm conventions of
app/engines/options/stt_scanners.py (Wilder RSI = ewm(alpha=1/period,
adjust=False, min_periods=period); MACD = ewm(span=..., adjust=False)) so
numbers agree across the codebase — but every function here returns full
Series, because Saro 1.2 needs *crossings* (RSI over 65, close over the
upper Keltner), not just the latest value.

TTM-squeeze definition (spec Section 2, verbatim):
  squeeze ON  <=> Bollinger(BB_PERIOD, BB_STD) fully INSIDE
                  Keltner(KC_PERIOD, KC_ATR_MULT x ATR(KC_ATR_PERIOD))
  ALERT       <=> squeeze active AND RVOL >= RVOL_MIN
                  AND price breaks above the upper Keltner line
"breaks above" is pinned as a transition: prior close <= prior KC-upper AND
current close > current KC-upper, with the squeeze active on the PRIOR bar
(the breakout bar itself usually blows the bands open, so requiring squeeze
on the breakout bar would make the alert nearly unfireable).
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from app.engines.scanner.saro12 import config as C


# ── bar shaping ────────────────────────────────────────────────────────────
def bars_to_df(bars: Any) -> Optional[pd.DataFrame]:
    """FMP fetch_intraday_bars dicts ({'t','o','h','l','c','v'}, oldest ->
    newest) or a ready DataFrame -> OHLCV DataFrame. None when unusable."""
    if bars is None:
        return None
    if isinstance(bars, pd.DataFrame):
        return bars if len(bars) else None
    rows = []
    for b in bars:
        try:
            rows.append({
                "open": float(b.get("o", 0)), "high": float(b.get("h", 0)),
                "low": float(b.get("l", 0)), "close": float(b.get("c", 0)),
                "volume": float(b.get("v", 0)),
            })
        except Exception:
            continue
    if not rows:
        return None
    return pd.DataFrame(rows)


# ── core indicators (all return Series aligned to the input index) ─────────
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(closes: pd.Series, period: int = C.RSI_PERIOD) -> pd.Series:
    """Wilder RSI — same smoothing as stt_scanners._rsi, full Series out."""
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # all-gain stretches (avg_loss == 0) -> RSI 100 by convention
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return out


def macd(closes: pd.Series, fast: int = C.MACD_FAST, slow: int = C.MACD_SLOW,
         signal: int = C.MACD_SIGNAL) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(macd_line, signal_line, histogram) — histogram = macd - signal."""
    macd_line = ema(closes, fast) - ema(closes, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def bollinger(closes: pd.Series, period: int = C.BB_PERIOD,
              num_std: float = C.BB_STD) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(mid, upper, lower). Population std (ddof=0) — the common BB choice."""
    mid = closes.rolling(period, min_periods=period).mean()
    sd = closes.rolling(period, min_periods=period).std(ddof=0)
    return mid, mid + num_std * sd, mid - num_std * sd


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    pc = close.shift(1)
    return pd.concat([(high - low).abs(), (high - pc).abs(),
                      (low - pc).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = C.KC_ATR_PERIOD) -> pd.Series:
    """Wilder-smoothed ATR Series (levels._atr is a tail-mean scalar — the
    Keltner channel needs a Series, so this is implemented fresh)."""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def keltner(high: pd.Series, low: pd.Series, close: pd.Series,
            period: int = C.KC_PERIOD, atr_mult: float = C.KC_ATR_MULT,
            atr_period: int = C.KC_ATR_PERIOD
            ) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(mid, upper, lower): mid = EMA(close, period), band = atr_mult * ATR."""
    mid = ema(close, period)
    band = atr_mult * atr(high, low, close, atr_period)
    return mid, mid + band, mid - band


def rvol(today_volume: Optional[float], avg_volume: Optional[float]) -> Optional[float]:
    """day_rvol = today's cumulative volume / 3-month avg daily volume.
    None when the denominator is missing/zero (caller decides the policy)."""
    try:
        tv = float(today_volume or 0)
        av = float(avg_volume or 0)
    except Exception:
        return None
    if av <= 0 or tv < 0:
        return None
    return tv / av


# ── composed signals ───────────────────────────────────────────────────────
def ttm_squeeze_on(high: pd.Series, low: pd.Series, close: pd.Series,
                   bb_period: int = C.BB_PERIOD, bb_std: float = C.BB_STD,
                   kc_period: int = C.KC_PERIOD, kc_atr_mult: float = C.KC_ATR_MULT,
                   kc_atr_period: int = C.KC_ATR_PERIOD) -> pd.Series:
    """Boolean Series: True where BB is fully inside the Keltner channel."""
    _, bb_up, bb_lo = bollinger(close, bb_period, bb_std)
    _, kc_up, kc_lo = keltner(high, low, close, kc_period, kc_atr_mult, kc_atr_period)
    on = (bb_up < kc_up) & (bb_lo > kc_lo)
    return on.fillna(False)


def rsi_cross_above(rsi_series: pd.Series, level: float = C.RSI_CROSS_LEVEL,
                    lookback: int = C.RSI_CROSS_LOOKBACK_BARS) -> bool:
    """True when RSI crossed above `level` within the last `lookback` bars
    (prev <= level < cur) AND the latest RSI is still above the level."""
    r = rsi_series.dropna()
    if len(r) < 2:
        return False
    if float(r.iloc[-1]) <= level:
        return False
    prev, cur = r.shift(1), r
    crossed = (prev <= level) & (cur > level)
    return bool(crossed.tail(max(1, int(lookback))).any())


def momentum_screen_ok(closes: pd.Series) -> tuple[bool, dict]:
    """Section 1 momentum gate: RSI(14) crossing above 65 WITH positive MACD
    histogram. Returns (ok, detail) — detail feeds quality_reasons."""
    detail: dict = {"rsi": None, "macd_hist": None}
    closes = closes.dropna()
    if len(closes) < C.MIN_BARS_FOR_SIGNAL:
        detail["note"] = f"insufficient bars ({len(closes)} < {C.MIN_BARS_FOR_SIGNAL})"
        return False, detail
    r = rsi(closes)
    _, _, hist = macd(closes)
    try:
        detail["rsi"] = round(float(r.dropna().iloc[-1]), 2)
        detail["macd_hist"] = round(float(hist.iloc[-1]), 6)
    except Exception:
        return False, detail
    ok = rsi_cross_above(r) and detail["macd_hist"] is not None and detail["macd_hist"] > 0
    return bool(ok), detail


def alert_state(df: Optional[pd.DataFrame], rvol_value: Optional[float]) -> dict:
    """Section 2 trigger on 1-min OHLCV bars. Returns a dict:
      {alert, squeeze_prev, breakout, rvol_ok, close, bar_low, kc_upper,
       kc_lower, bars} — `alert` only when ALL THREE spec legs hold.
    Never raises; unusable bars -> alert False."""
    out = {"alert": False, "squeeze_prev": False, "breakout": False,
           "rvol_ok": False, "close": None, "bar_low": None,
           "kc_upper": None, "kc_lower": None, "bars": 0}
    try:
        if df is None or len(df) < C.MIN_BARS_FOR_SIGNAL:
            out["bars"] = 0 if df is None else int(len(df))
            return out
        out["bars"] = int(len(df))
        h, l, c = df["high"], df["low"], df["close"]
        sq = ttm_squeeze_on(h, l, c)
        _, kc_up, kc_lo = keltner(h, l, c)
        if pd.isna(kc_up.iloc[-1]) or pd.isna(kc_up.iloc[-2]):
            return out
        close_cur, close_prev = float(c.iloc[-1]), float(c.iloc[-2])
        up_cur, up_prev = float(kc_up.iloc[-1]), float(kc_up.iloc[-2])
        out["close"] = close_cur
        out["bar_low"] = float(l.iloc[-1])
        out["kc_upper"] = up_cur
        out["kc_lower"] = None if pd.isna(kc_lo.iloc[-1]) else float(kc_lo.iloc[-1])
        out["squeeze_prev"] = bool(sq.iloc[-2])
        out["breakout"] = bool(close_prev <= up_prev and close_cur > up_cur)
        out["rvol_ok"] = bool(rvol_value is not None and rvol_value >= C.RVOL_MIN)
        out["alert"] = out["squeeze_prev"] and out["breakout"] and out["rvol_ok"]
        return out
    except Exception:
        return out
