"""Structure-based entry/stop/target for the scanner (SCANNER-LEVELS-V1).

Replaces the fixed -3% stop / +10% target placeholders with levels derived from
real chart structure, with a measured-move target. Design notes:

* Stops prefer MAJOR structure (pre-market / prior-day / session highs+lows from
  level_reasons._session_levels) over 1-minute micro-swings, and are bounded to
  [min_stop_pct, max_stop_pct] from entry so a 1-min wiggle can't create a
  hair-tight stop that noise instantly trips. ATR (floored at min_stop_pct) is
  the fallback when no clean structure sits in range.
* Targets are a MEASURED MOVE (rr x structural risk) — a structure-anchored,
  volatility-scaled target — optionally extended to a major overhead level when
  one sits beyond the measured move (so a strong setup isn't capped short).
* Returns ok=False when no sane stop / minimum R:R can be formed, so the caller
  down-ranks to watch-only / NO-TRADE instead of forcing a fabricated level.

Bars: a list of Polygon 1-min aggregate dicts ({t,o,h,l,c,v}) from
premarket_scheduler._polygon_1min_bars, OR a pandas DataFrame. daily_bars (same
shapes) add multi-day structure for swing setups.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.engines.level_reasons import _swings, _session_levels


@dataclass
class Levels:
    entry: float
    stop: float
    target: float
    rr: float
    projected_move_pct: float
    stop_reason: str
    target_reason: str
    basis: str            # "structure" | "atr_fallback"
    ok: bool              # False -> no sane stop / R:R -> caller should watch-only / no-trade
    detail: dict = field(default_factory=dict)


def _bars_to_df(bars: Any):
    import pandas as pd
    if bars is None:
        return None
    if isinstance(bars, pd.DataFrame):
        return bars if len(bars) else None
    rows = []
    for b in bars:
        try:
            rows.append({
                "timestamp": pd.to_datetime(int(b["t"]), unit="ms", utc=True),
                "open": float(b.get("o", 0)), "high": float(b.get("h", 0)),
                "low": float(b.get("l", 0)), "close": float(b.get("c", 0)),
                "volume": float(b.get("v", 0)),
            })
        except Exception:
            continue
    if not rows:
        return None
    return pd.DataFrame(rows).set_index("timestamp")


def _atr(df, period: int = 14) -> Optional[float]:
    try:
        import pandas as pd
        h, l, c = df["high"], df["low"], df["close"]
        pc = c.shift(1)
        tr = pd.concat([(h - l).abs(), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        v = float(tr.tail(period).mean())
        return v if v and v > 0 else None
    except Exception:
        return None


def _atr_proxy_pct(price: float) -> float:
    if price < 2:   return 0.05
    if price < 10:  return 0.03
    if price < 50:  return 0.02
    if price < 200: return 0.015
    return 0.012


def _session_hl(df, want_low: bool) -> list:
    """Major session / prior-day highs or lows from _session_levels()."""
    out = []
    try:
        sl = _session_levels(df) or {}
    except Exception:
        sl = {}
    for label, val in sl.items():
        try:
            kind, price = val
        except Exception:
            continue
        if want_low and kind == "low":
            out.append((float(price), label))
        elif (not want_low) and kind == "high":
            out.append((float(price), label))
    return out


def _collect(df, tag, entry):
    """Return (major_supports, major_resists, swing_supports, swing_resists) as
    (level, reason) lists relative to entry. Majors = session/prior-day levels."""
    maj_s, maj_r, sw_s, sw_r = [], [], [], []
    if df is None or len(df) < 2:
        return maj_s, maj_r, sw_s, sw_r
    try:
        s_hi, s_lo = _swings(df)
    except Exception:
        s_hi, s_lo = [], []
    for lvl in s_lo:
        if lvl and lvl < entry:
            sw_s.append((float(lvl), f"{tag} swing low"))
    for lvl in s_hi:
        if lvl and lvl > entry:
            sw_r.append((float(lvl), f"{tag} swing high"))
    for lvl, lab in _session_hl(df, want_low=True):
        if lvl < entry:
            maj_s.append((lvl, lab))
    for lvl, lab in _session_hl(df, want_low=False):
        if lvl > entry:
            maj_r.append((lvl, lab))
    return maj_s, maj_r, sw_s, sw_r


def compute_levels(direction: str, price: float, bars=None, *, daily_bars=None,
                   rr: float = 2.0, atr_period: int = 14, atr_stop_mult: float = 1.5,
                   min_rr: float = 1.5, min_stop_pct: float = 0.015,
                   max_stop_pct: float = 0.12) -> Levels:
    direction = (direction or "long").lower()
    entry = float(price)
    df = _bars_to_df(bars)
    ddf = _bars_to_df(daily_bars)

    maj_s, maj_r, sw_s, sw_r = [], [], [], []
    for src, tag in ((df, "intraday"), (ddf, "daily")):
        a, b, c, d = _collect(src, tag, entry)
        maj_s += a; maj_r += b; sw_s += c; sw_r += d
    atr = _atr(df, atr_period) or _atr(ddf, atr_period)

    def _dist_ok(lvl):  # within [min_stop_pct, max_stop_pct] of entry
        d = abs(entry - lvl) / entry
        return min_stop_pct <= d <= max_stop_pct

    # ---- stop: prefer MAJOR structure in range, then meaningful swings, then ATR ----
    stop = None
    stop_reason = None
    basis = "structure"
    if direction == "long":
        pool = [(l, w) for l, w in maj_s if _dist_ok(l)] or [(l, w) for l, w in sw_s if _dist_ok(l)]
        if pool:
            lvl, stop_reason = max(pool, key=lambda x: x[0])   # nearest valid support below
            stop = round(lvl * 0.999, 2)
    else:
        pool = [(l, w) for l, w in maj_r if _dist_ok(l)] or [(l, w) for l, w in sw_r if _dist_ok(l)]
        if pool:
            lvl, stop_reason = min(pool, key=lambda x: x[0])
            stop = round(lvl * 1.001, 2)
    if stop is None:
        dist = max((atr * atr_stop_mult) if atr else 0.0, entry * min_stop_pct)
        stop = round(entry - dist, 2) if direction == "long" else round(entry + dist, 2)
        stop_reason = ((f"ATR({atr_period})x{atr_stop_mult:g}" if atr
                        else f"{_atr_proxy_pct(entry) * 100:.1f}% ATR-proxy")
                       + " stop (no clean structure in range)")
        basis = "atr_fallback"

    risk = abs(entry - stop)
    if risk <= 0:
        return Levels(round(entry, 2), round(stop, 2), round(entry, 2), 0.0, 0.0,
                      stop_reason or "", "", basis, False, {"error": "non-positive risk"})

    # ---- target: measured move (rr x risk), extended to a major level beyond it ----
    if direction == "long":
        target = entry + rr * risk
        target_reason = f"measured move {rr:g}R from {stop_reason}"
        ext = [(l, w) for l, w in maj_r if l > target]
        if ext:
            lvl, why = min(ext, key=lambda x: x[0])
            if (lvl - entry) / entry <= 0.30:   # don't chase absurd targets
                target, target_reason = round(lvl, 2), f"{why} (next major resistance, {rr:g}R+)"
    else:
        target = entry - rr * risk
        target_reason = f"measured move {rr:g}R from {stop_reason}"
        ext = [(l, w) for l, w in maj_s if l < target]
        if ext:
            lvl, why = max(ext, key=lambda x: x[0])
            if (entry - lvl) / entry <= 0.30:
                target, target_reason = round(lvl, 2), f"{why} (next major support, {rr:g}R+)"

    real_rr = round(abs(target - entry) / risk, 2)
    proj = round(abs(target - entry) / entry * 100.0, 2)
    return Levels(round(entry, 2), round(stop, 2), round(target, 2), real_rr, proj,
                  stop_reason, target_reason, basis, real_rr >= min_rr,
                  {"n_major_s": len(maj_s), "n_major_r": len(maj_r),
                   "n_swing_s": len(sw_s), "n_swing_r": len(sw_r), "atr": atr,
                   "stop_pct": round(risk / entry * 100, 2)})
