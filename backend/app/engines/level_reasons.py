"""Level-reason inference for stop-loss / take-profit labels.

Given a trade's geometry (direction, entry, stop, target) plus recent OHLC
bars and the current time, infer a *human-readable* reason for each level —
e.g. "swing low", "previous high", "London high", "session VWAP",
"FVG invalidation" — so emails, charts and the Email Signals page can show
WHY a stop/target sits where it does instead of a bare price.

Design goals
------------
* **One central helper.** Strategies are NOT edited; we infer the reason
  post-hoc from the price relative to detected market levels.
* **Never raise.** Missing/short/garbage bars → fall back to the generic
  "strategy stop" / "strategy target" labels. A labelling helper must never
  break the signal pipeline.
* **Never blank.** Every return always contains non-empty strings.

Levels detected (each compared against stop & target within a tolerance):
  - swing low / swing high      (5-bar pivots — local extrema)
  - previous day high / low     (from prior ET calendar day's bars)
  - session highs / lows:
        Asian / Globex range     (18:00–02:00 ET)
        London                   (02:00–05:00 ET)
        NY AM                    (09:30–11:00 ET)
  - session VWAP                 (typical-price * volume, cumulative)
  - FVG zone edges               (3-candle fair-value-gap invalidation)

Tolerance: a price is "at" a level when ``abs(price-level)/price < 0.0015``
(15 bps) OR within ~3 ticks for known futures instruments — whichever is
looser. For each of stop and target we pick the CLOSEST matching level whose
direction makes sense (stops skew to the protective side, targets to the
objective side), then fall back to the generic label if nothing matches.
"""
from __future__ import annotations

from typing import Optional

# Default match tolerance as a fraction of price (15 bps).
_TOL_FRAC = 0.0015

# Per-instrument tick sizes for the "within ~3 ticks" alternate tolerance.
# Keyed by the leading alpha root so ES, ESU5, "ES=F", MES all resolve.
_TICK_SIZE = {
    "ES": 0.25, "MES": 0.25, "NQ": 0.25, "MNQ": 0.25,
    "RTY": 0.10, "M2K": 0.10, "YM": 1.0, "MYM": 1.0,
    "CL": 0.01, "MCL": 0.01, "GC": 0.10, "MGC": 0.10,
    "SI": 0.005, "NG": 0.001, "ZB": 1.0 / 32.0, "ZN": 1.0 / 64.0,
    "6E": 0.00005, "6J": 0.0000005, "BTC": 5.0, "MBT": 5.0, "ETH": 0.5,
}
_TICKS_TOLERANCE = 3.0


def _root_symbol(instrument: Optional[str]) -> str:
    """Leading alpha run of an instrument symbol, upper-cased.
    'ESU5' -> 'ES', 'MES=F' -> 'MES', 'cl' -> 'CL', '' -> ''."""
    if not instrument:
        return ""
    s = str(instrument).upper().strip()
    out = []
    for ch in s:
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out)


def _tolerance_for(price: float, instrument: Optional[str]) -> float:
    """Absolute price tolerance: the looser of 15 bps and ~3 ticks."""
    try:
        frac_tol = abs(float(price)) * _TOL_FRAC
    except (TypeError, ValueError):
        return 0.0
    tick = _TICK_SIZE.get(_root_symbol(instrument))
    if tick:
        return max(frac_tol, tick * _TICKS_TOLERANCE)
    return frac_tol


def _matches(price: float, level: float, tol: float) -> bool:
    try:
        return abs(float(price) - float(level)) <= tol
    except (TypeError, ValueError):
        return False


def _normalize_bars(bars_df):
    """Return a DataFrame with lowercase open/high/low/close[/volume] columns
    and, when available, a tz-aware UTC DatetimeIndex. Returns None if the
    frame is missing, empty, or lacks OHLC columns. Never raises."""
    if bars_df is None:
        return None
    try:
        import pandas as pd
    except Exception:
        return None
    try:
        df = bars_df.copy()
    except Exception:
        return None
    if getattr(df, "empty", True):
        return None
    rename = {}
    for c in list(df.columns):
        lc = str(c).lower()
        if lc in ("open", "high", "low", "close", "volume", "timestamp", "vwap", "vw"):
            rename[c] = lc
    if rename:
        df = df.rename(columns=rename)
    # Promote a timestamp column to the index if present.
    if "timestamp" in df.columns:
        try:
            df = df.set_index(pd.DatetimeIndex(pd.to_datetime(df["timestamp"], utc=True)))
        except Exception:
            pass
    else:
        # Try to coerce an existing index to datetime (tolerate failure).
        try:
            df.index = pd.to_datetime(df.index, utc=True)
        except Exception:
            pass
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            return None
    return df


def _et_index(df):
    """Return a DatetimeIndex localized to America/New_York, or None if the
    index is not datetime-like. Never raises."""
    try:
        import pandas as pd
    except Exception:
        return None
    idx = df.index
    if not isinstance(idx, pd.DatetimeIndex):
        return None
    try:
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        return idx.tz_convert("America/New_York")
    except Exception:
        return None


def _swings(df, lookback: int = 2):
    """Detect swing highs/lows as n-bar pivots: a bar whose high is the max
    (low is the min) of the window [i-lookback, i+lookback]. Returns
    (swing_highs, swing_lows) as lists of floats. Defensive on short frames."""
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    n = len(highs)
    s_hi, s_lo = [], []
    if n < (2 * lookback + 1):
        # Too short for a pivot window — fall back to the absolute extrema so a
        # tight setup still resolves "swing high/low".
        if n:
            s_hi.append(max(highs))
            s_lo.append(min(lows))
        return s_hi, s_lo
    for i in range(lookback, n - lookback):
        win_hi = highs[i - lookback:i + lookback + 1]
        win_lo = lows[i - lookback:i + lookback + 1]
        if highs[i] >= max(win_hi):
            s_hi.append(highs[i])
        if lows[i] <= min(win_lo):
            s_lo.append(lows[i])
    # Always include the most recent bar's extreme as a candidate — many stops
    # sit at the very last swing which the centred window can't confirm yet.
    if highs:
        s_hi.append(highs[-1])
        s_lo.append(lows[-1])
    return s_hi, s_lo


def _session_levels(df):
    """Compute previous-day and intraday-session highs/lows from ET-localized
    timestamps. Returns a dict mapping a human label -> ("high"/"low", value).

    Sessions (ET):
      Asian / Globex   18:00–02:00 (wraps midnight)
      London           02:00–05:00
      NY AM            09:30–11:00
    Previous day = the most recent ET calendar date strictly before the latest
    bar's ET date. Returns {} when timestamps aren't usable."""
    et = _et_index(df)
    if et is None:
        return {}
    try:
        import pandas as pd
    except Exception:
        return {}
    highs = df["high"].astype(float).reset_index(drop=True)
    lows = df["low"].astype(float).reset_index(drop=True)
    mins = (et.hour * 60 + et.minute)
    dates = et.normalize()  # ET midnight per bar
    last_date = dates[-1]

    out: dict = {}

    def _hl(mask, hi_label, lo_label):
        try:
            import numpy as _np  # noqa
        except Exception:
            pass
        sel = [i for i, m in enumerate(mask) if m]
        if not sel:
            return
        hv = max(highs[i] for i in sel)
        lv = min(lows[i] for i in sel)
        out[hi_label] = ("high", hv)
        out[lo_label] = ("low", lv)

    # Previous-day high/low — bars whose ET date is the latest date present
    # that is strictly earlier than the last bar's date.
    prior_dates = [d for d in set(dates) if d < last_date]
    if prior_dates:
        prev_day = max(prior_dates)
        mask = [d == prev_day for d in dates]
        _hl(mask, "previous high", "previous low")

    # Today's session windows (relative to the last bar's ET date so an
    # overnight Asian session that began "yesterday" still groups correctly).
    today_mask = [d == last_date for d in dates]

    asian_mask = [today_mask[i] and (mins[i] >= 18 * 60 or mins[i] < 2 * 60)
                  for i in range(len(mins))]
    # Asian can legitimately span the prior ET evening; widen to include the
    # 18:00–24:00 bars from the day before the last bar too.
    yday = last_date - pd.Timedelta(days=1)
    asian_mask = [
        am or (dates[i] == yday and mins[i] >= 18 * 60)
        for i, am in enumerate(asian_mask)
    ]
    _hl(asian_mask, "Asian high", "Asian low")

    london_mask = [today_mask[i] and (2 * 60 <= mins[i] < 5 * 60)
                   for i in range(len(mins))]
    _hl(london_mask, "London high", "London low")

    ny_am_mask = [today_mask[i] and (9 * 60 + 30 <= mins[i] < 11 * 60)
                  for i in range(len(mins))]
    _hl(ny_am_mask, "NY AM high", "NY AM low")

    return out


def _session_vwap(df) -> Optional[float]:
    """Cumulative session VWAP from the last ET session's bars.
    typical = (h+l+c)/3 (or a 'vwap'/'vw' column when present), weighted by
    volume. Returns None if no volume is available. Never raises."""
    try:
        et = _et_index(df)
        highs = df["high"].astype(float).reset_index(drop=True)
        lows = df["low"].astype(float).reset_index(drop=True)
        closes = df["close"].astype(float).reset_index(drop=True)
        if "volume" in df.columns:
            vols = df["volume"].astype(float).reset_index(drop=True)
        else:
            return None
        # Restrict to the last ET calendar date when we can, else use all bars.
        idxs = range(len(closes))
        if et is not None:
            dates = et.normalize()
            last_date = dates[-1]
            idxs = [i for i in range(len(closes)) if dates[i] == last_date] or list(range(len(closes)))
        num = 0.0
        den = 0.0
        for i in idxs:
            v = float(vols[i])
            if v <= 0:
                continue
            tp = (float(highs[i]) + float(lows[i]) + float(closes[i])) / 3.0
            num += tp * v
            den += v
        return (num / den) if den > 0 else None
    except Exception:
        return None


def _fvg_zones(df):
    """3-candle fair-value-gap detection. Returns two lists of edge prices:
    (bullish_edges, bearish_edges).

    Bullish FVG: low[i] > high[i-2]  → gap between high[i-2] and low[i]; a long
      that holds the gap is invalidated if price closes back below high[i-2],
      so high[i-2] is the protective (stop-side) edge.
    Bearish FVG: high[i] < low[i-2]  → gap between low[i-2] and high[i]; the
      stop-side edge for a short is low[i-2].
    We surface the nearest edges; matching is done by the caller. Defensive."""
    bull, bear = [], []
    try:
        highs = df["high"].astype(float).tolist()
        lows = df["low"].astype(float).tolist()
        n = len(highs)
        for i in range(2, n):
            if lows[i] > highs[i - 2]:
                bull.append(highs[i - 2])  # lower edge — long invalidation
                bull.append(lows[i])       # upper edge
            if highs[i] < lows[i - 2]:
                bear.append(lows[i - 2])   # upper edge — short invalidation
                bear.append(highs[i])      # lower edge
    except Exception:
        return [], []
    return bull, bear


def _closest_label(price, candidates, tol):
    """From candidates = list of (label, level), return the label of the level
    closest to `price` that is within `tol`, else None."""
    best_label = None
    best_dist = None
    for label, level in candidates:
        try:
            d = abs(float(price) - float(level))
        except (TypeError, ValueError):
            continue
        if d <= tol and (best_dist is None or d < best_dist):
            best_dist = d
            best_label = label
    return best_label


def infer_stop_target_reasons(
    *,
    direction: str,
    entry: float,
    stop: float,
    target: float,
    bars_df,
    instrument: Optional[str] = None,
    now_utc=None,
) -> dict:
    """Infer human-readable reasons for a stop and target price.

    Returns ``{"stop_reason": str, "target_reason": str}``. Compares the stop
    and target against detected market levels (swings, previous-day high/low,
    Asian/London/NY-AM session highs/lows, session VWAP, FVG edges) and labels
    each with the CLOSEST level within tolerance. For a LONG, the stop skews to
    protective *lows* and the target to objective *highs* (mirrored for SHORT).

    Falls back to ``"strategy stop"`` / ``"strategy target"`` when nothing
    matches or the bars are unusable. NEVER raises and NEVER returns a blank
    label."""
    stop_reason = "strategy stop"
    target_reason = "strategy target"

    d = (direction or "").lower().strip()
    is_long = d in ("long", "buy")
    is_short = d in ("short", "sell")

    try:
        entry_f = float(entry)
        stop_f = float(stop)
        target_f = float(target)
    except (TypeError, ValueError):
        return {"stop_reason": stop_reason, "target_reason": target_reason}

    df = _normalize_bars(bars_df)
    if df is None:
        return {"stop_reason": stop_reason, "target_reason": target_reason}

    # ── Detect levels ────────────────────────────────────────────────────
    try:
        s_hi, s_lo = _swings(df)
    except Exception:
        s_hi, s_lo = [], []
    try:
        sessions = _session_levels(df)
    except Exception:
        sessions = {}
    try:
        vwap = _session_vwap(df)
    except Exception:
        vwap = None
    try:
        fvg_bull, fvg_bear = _fvg_zones(df)
    except Exception:
        fvg_bull, fvg_bear = [], []

    # ── Build candidate pools, directionally biased ──────────────────────
    # "low" levels are protective for longs / objective for shorts; vice-versa.
    # ORDER MATTERS: _closest_label keeps the first candidate on a distance
    # tie, so we list the more *specific / named* levels first (previous-day
    # and session highs/lows, then FVG, then VWAP) and the generic swing
    # high/low LAST. That way a price sitting at both a swing high and the
    # previous-day high is labelled "previous high", which is the more
    # informative reason.
    low_candidates = []   # things at the downside
    high_candidates = []  # things at the upside

    # 1. Named session / previous-day levels (most specific).
    for label, (side, val) in sessions.items():
        if side == "low":
            low_candidates.append((label, val))
        else:
            high_candidates.append((label, val))

    # 2. FVG invalidation edges.
    for v in fvg_bull:
        low_candidates.append(("FVG invalidation", v))
    for v in fvg_bear:
        high_candidates.append(("FVG invalidation", v))

    # 3. Session VWAP.
    if vwap is not None:
        low_candidates.append(("session VWAP", vwap))
        high_candidates.append(("session VWAP", vwap))

    # 4. Generic swing pivots (least specific — only wins when nothing named
    #    is closer).
    for v in s_lo:
        low_candidates.append(("swing low", v))
    for v in s_hi:
        high_candidates.append(("swing high", v))

    # ── Assemble per-leg candidate ordering by side ──────────────────────
    if is_long:
        stop_pool = low_candidates
        target_pool = high_candidates
    elif is_short:
        stop_pool = high_candidates
        target_pool = low_candidates
    else:
        # Unknown direction — consider everything for both legs.
        stop_pool = low_candidates + high_candidates
        target_pool = high_candidates + low_candidates

    stop_tol = _tolerance_for(stop_f, instrument)
    target_tol = _tolerance_for(target_f, instrument)

    m = _closest_label(stop_f, stop_pool, stop_tol)
    if m:
        stop_reason = m
    m = _closest_label(target_f, target_pool, target_tol)
    if m:
        target_reason = m

    return {"stop_reason": stop_reason, "target_reason": target_reason}
