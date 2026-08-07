"""Saro 1.3 indicators — pure pandas/numpy on DAILY bars, deterministic.

NO TA-Lib in the image. RSI uses the exact Wilder ewm convention of
saro12/indicators.py (ewm(alpha=1/period, adjust=False,
min_periods=period)) so numbers agree across cohorts.

Pinned definitions (S1 + S2, tested in tests/test_saro13.py):
  BB width          = (upperBB - lowerBB) / middleBB on daily Bollinger(20, 2)
  lowest-15%-of-60d = current width <= 15th percentile (numpy linear
                      interpolation) of the trailing 60 width values,
                      window INCLUSIVE of the current session
  ATR decline       = ATR(14, Wilder) STRICTLY decreasing across the last
                      5 values (a[-5] > a[-4] > a[-3] > a[-2] > a[-1])
  RSI band          = daily RSI(14) in [45, 55]; > 65 is the spec's hard
                      reject (implied by the band, logged explicitly)
  Volume contraction= last completed session volume < SMA20(volume),
                      window inclusive of that session
  Daily change      = > +3% vs prior close rejects (positive moves only —
                      the coil must not already be breaking)

LIVE-PARTIAL RULE (probed 2026-08-07): FMP /stable/historical-price-eod/full
returns today's row intraday as a LIVE PARTIAL and the list arrives
newest-first, as a bare list (no {'historical': ...} wrapper — but a silent
shape flip is a known FMP failure mode, so both are tolerated).
daily_rows_to_df() therefore sorts by date desc explicitly, DROPS every row
dated >= today ET, and returns oldest -> newest. All compression math runs
on COMPLETED sessions only.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

from app.engines.scanner.saro13 import config as C


# ── bar shaping ────────────────────────────────────────────────────────────
def daily_rows_to_df(rows: Any, today_et: str) -> Optional[pd.DataFrame]:
    """FMP /stable EOD rows ({'date','open','high','low','close','volume'},
    bare list OR wrapped {'historical': [...]} dict) -> OHLCV DataFrame,
    oldest -> newest, COMPLETED sessions only (rows dated >= today_et are a
    live partial and are dropped). None when unusable."""
    if isinstance(rows, dict):
        rows = rows.get("historical")  # tolerate the wrapped variant
    if not rows:
        return None
    parsed = []
    for r0 in rows:
        if not isinstance(r0, dict):
            continue
        try:
            d = str(r0.get("date") or "")[:10]
            if not d or d >= str(today_et):
                continue  # today's row = live partial, never a baseline
            parsed.append({
                "date": d,
                "open": float(r0.get("open", 0)), "high": float(r0.get("high", 0)),
                "low": float(r0.get("low", 0)), "close": float(r0.get("close", 0)),
                "volume": float(r0.get("volume", 0)),
            })
        except Exception:
            continue
    if not parsed:
        return None
    # never trust upstream ordering (probed newest-first; a silent flip would
    # scramble every rolling window): sort desc explicitly, then reverse.
    parsed.sort(key=lambda r0: r0["date"], reverse=True)
    parsed.reverse()
    return pd.DataFrame(parsed)


# ── core indicators (Series in, Series out, aligned to input index) ────────
def rsi(closes: pd.Series, period: int = C.RSI_PERIOD) -> pd.Series:
    """Wilder RSI — same smoothing as saro12/indicators.rsi."""
    delta = closes.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return out


def bollinger(closes: pd.Series, period: int = C.BB_PERIOD,
              num_std: float = C.BB_STD) -> tuple[pd.Series, pd.Series, pd.Series]:
    """(mid, upper, lower). Population std (ddof=0) — the common BB choice."""
    mid = closes.rolling(period, min_periods=period).mean()
    sd = closes.rolling(period, min_periods=period).std(ddof=0)
    return mid, mid + num_std * sd, mid - num_std * sd


def bb_width(closes: pd.Series, period: int = C.BB_PERIOD,
             num_std: float = C.BB_STD) -> pd.Series:
    """PINNED: width = (upperBB - lowerBB) / middleBB on daily(20,2)."""
    mid, up, lo = bollinger(closes, period, num_std)
    return (up - lo) / mid.replace(0.0, np.nan)


def bbwidth_in_lowest_pct(width: pd.Series, pct: float = C.BBWIDTH_LOWEST_PCT,
                          sessions: int = C.BBWIDTH_RANGE_SESSIONS
                          ) -> tuple[bool, dict]:
    """PINNED: True when the current width <= the `pct`th percentile of the
    trailing `sessions` width values (inclusive of the current one)."""
    detail: dict = {"bb_width": None, "pct_threshold": None}
    w = width.dropna()
    if len(w) < sessions:
        detail["note"] = f"insufficient width history ({len(w)} < {sessions})"
        return False, detail
    window = w.iloc[-sessions:].to_numpy(dtype=float)
    cur = float(window[-1])
    thresh = float(np.percentile(window, pct))
    detail["bb_width"] = round(cur, 6)
    detail["pct_threshold"] = round(thresh, 6)
    return bool(cur <= thresh), detail


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    pc = close.shift(1)
    return pd.concat([(high - low).abs(), (high - pc).abs(),
                      (low - pc).abs()], axis=1).max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series,
        period: int = C.ATR_PERIOD) -> pd.Series:
    """Wilder-smoothed ATR(14) Series on daily bars."""
    tr = true_range(high, low, close)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def atr_declining(atr_series: pd.Series,
                  sessions: int = C.ATR_DECLINE_SESSIONS) -> bool:
    """PINNED: STRICTLY decreasing across the last `sessions` ATR values —
    every consecutive diff < 0. Insufficient history -> False."""
    a = atr_series.dropna()
    if len(a) < sessions:
        return False
    tail = a.iloc[-sessions:].to_numpy(dtype=float)
    return bool(np.all(np.diff(tail) < 0))


def vol_contraction(volumes: pd.Series,
                    period: int = C.VOL_SMA_PERIOD) -> tuple[bool, dict]:
    """PINNED: last completed session volume < SMA20(volume), inclusive
    window. Returns (ok, {vol, vol_sma, vol_ratio})."""
    detail: dict = {"vol": None, "vol_sma": None, "vol_ratio": None}
    v = volumes.dropna()
    if len(v) < period:
        detail["note"] = f"insufficient volume history ({len(v)} < {period})"
        return False, detail
    sma = v.rolling(period, min_periods=period).mean()
    cur, cur_sma = float(v.iloc[-1]), float(sma.iloc[-1])
    detail["vol"] = cur
    detail["vol_sma"] = round(cur_sma, 1)
    detail["vol_ratio"] = round(cur / cur_sma, 3) if cur_sma > 0 else None
    return bool(cur_sma > 0 and cur < cur_sma), detail


def passes_daily_change(price: Optional[float], prev_close: Optional[float],
                        max_pct: float = C.DAILY_CHANGE_MAX_PCT
                        ) -> tuple[bool, Optional[float]]:
    """S1 reject: daily change > +max_pct vs prior close. POSITIVE moves only
    (a red day is not a breakout). Unknown inputs -> pass with None pct (the
    completed-session change gate in compression_state still applies)."""
    try:
        p = float(price) if price is not None else None
        pc = float(prev_close) if prev_close is not None else None
    except Exception:
        return True, None
    if p is None or pc is None or pc <= 0:
        return True, None
    pct = (p / pc - 1.0) * 100.0
    return bool(pct <= max_pct), round(pct, 3)


def swing_low(lows: pd.Series, lookback: int = C.SWING_LOW_LOOKBACK
              ) -> Optional[float]:
    """Last-swing-low proxy: min low of the last `lookback` completed
    sessions. None when unavailable."""
    v = lows.dropna()
    if not len(v):
        return None
    return float(v.iloc[-max(1, int(lookback)):].min())


# ── composed S1 + S2 state ─────────────────────────────────────────────────
def compression_state(df: Optional[pd.DataFrame]) -> dict:
    """Full S1+S2 evaluation on a COMPLETED-sessions daily DataFrame.
    Returns a dict; `compressed` is True only when ALL legs hold:
      S1: BB width in lowest 15% of 60d  |  ATR(14) strictly down 5 sessions
          | last completed session change <= +3%
      S2: RSI(14) in [45,55] (hard > 65 flagged)  |  volume < SMA20(volume)
    The LIVE-session +3% reject needs a live price and lives in screen.py
    (passes_daily_change) — both gates are spec S1, split by data source.
    Never raises; unusable input -> compressed False with a note."""
    out: dict = {"compressed": False, "bars": 0, "reject_reasons": [],
                 "close": None, "prev_close": None, "last_chg_pct": None,
                 "upper_bb": None, "lower_bb": None, "mid_bb": None,
                 "bb_width": None, "bb_width_thresh": None, "bb_width_ok": False,
                 "atr": None, "atr_declining_ok": False,
                 "rsi": None, "rsi_ok": False, "rsi_hard_reject": False,
                 "vol": None, "vol_sma": None, "vol_ratio": None, "vol_ok": False,
                 "swing_low": None, "compression_range": None}
    try:
        if df is None or len(df) < C.MIN_DAILY_SESSIONS:
            out["bars"] = 0 if df is None else int(len(df))
            out["reject_reasons"].append(
                f"insufficient daily history ({out['bars']} < {C.MIN_DAILY_SESSIONS})")
            return out
        out["bars"] = int(len(df))
        closes, highs, lows, vols = df["close"], df["high"], df["low"], df["volume"]

        close_cur = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        out["close"], out["prev_close"] = close_cur, prev_close
        chg = (close_cur / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0
        out["last_chg_pct"] = round(chg, 3)
        chg_ok = chg <= C.DAILY_CHANGE_MAX_PCT
        if not chg_ok:
            out["reject_reasons"].append(
                f"daily change {chg:+.2f}% > +{C.DAILY_CHANGE_MAX_PCT:g}% vs prior close "
                f"(last completed session — move already started)")

        mid, up, lo = bollinger(closes)
        out["mid_bb"] = round(float(mid.iloc[-1]), 4)
        out["upper_bb"] = round(float(up.iloc[-1]), 4)
        out["lower_bb"] = round(float(lo.iloc[-1]), 4)
        out["compression_range"] = round(float(up.iloc[-1] - lo.iloc[-1]), 4)

        w_ok, w_det = bbwidth_in_lowest_pct(bb_width(closes))
        out["bb_width"] = w_det.get("bb_width")
        out["bb_width_thresh"] = w_det.get("pct_threshold")
        out["bb_width_ok"] = bool(w_ok)
        if not w_ok:
            out["reject_reasons"].append(
                f"BB width {w_det.get('bb_width')} not in lowest "
                f"{C.BBWIDTH_LOWEST_PCT:g}% of {C.BBWIDTH_RANGE_SESSIONS}d range "
                f"(p{C.BBWIDTH_LOWEST_PCT:g}={w_det.get('pct_threshold')}; "
                f"{w_det.get('note', 'no coil')})")

        a = atr(highs, lows, closes)
        try:
            out["atr"] = round(float(a.dropna().iloc[-1]), 4)
        except Exception:
            pass
        a_ok = atr_declining(a)
        out["atr_declining_ok"] = bool(a_ok)
        if not a_ok:
            out["reject_reasons"].append(
                f"ATR({C.ATR_PERIOD}) not strictly declining "
                f"{C.ATR_DECLINE_SESSIONS} consecutive sessions")

        r = rsi(closes).dropna()
        r_last = float(r.iloc[-1]) if len(r) else None
        out["rsi"] = round(r_last, 2) if r_last is not None else None
        r_ok = r_last is not None and C.RSI_BAND_LO <= r_last <= C.RSI_BAND_HI
        out["rsi_ok"] = bool(r_ok)
        out["rsi_hard_reject"] = bool(r_last is not None and r_last > C.RSI_HARD_REJECT)
        if not r_ok:
            tag = " HARD REJECT" if out["rsi_hard_reject"] else ""
            out["reject_reasons"].append(
                f"RSI({C.RSI_PERIOD}) {out['rsi']} outside "
                f"[{C.RSI_BAND_LO:g},{C.RSI_BAND_HI:g}]{tag}")

        v_ok, v_det = vol_contraction(vols)
        out["vol"], out["vol_sma"] = v_det.get("vol"), v_det.get("vol_sma")
        out["vol_ratio"] = v_det.get("vol_ratio")
        out["vol_ok"] = bool(v_ok)
        if not v_ok:
            out["reject_reasons"].append(
                f"volume {v_det.get('vol')} not below SMA{C.VOL_SMA_PERIOD} "
                f"{v_det.get('vol_sma')} (no contraction)")

        out["swing_low"] = swing_low(lows)
        out["compressed"] = bool(chg_ok and w_ok and a_ok and r_ok and v_ok)
        return out
    except Exception as e:
        out["reject_reasons"].append(f"compression_state error: {e}")
        return out
