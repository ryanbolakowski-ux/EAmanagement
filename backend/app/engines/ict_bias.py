"""Drop-in module: ICT-style daily bias.

Builds a richer "where are we in the day" reading than the prior 30D-EMA
crossover. Output combines:

  • 30D EMA trend (kept as `trend` / `trend_strength_pct` — context only)
  • PDH / PDL / PDC (prior RTH session 09:30-16:00 ET high/low/close)
  • Position vs prior day  → above_pdh / below_pdl / inside
  • Opening type           → gap_up / gap_down / inside
  • Asian session range (18:00 ET yest → 03:00 ET today) + sweep flags
  • Current session label  → asian / london / ny / overnight
  • Draw on liquidity      → nearest unmitigated level the chart is "pulled" toward
  • Intraday bias headline → bullish / bearish / neutral synthesised from above
  • Plain-English narrative

The `bias` and `strength_pct` fields keep their original meanings for
backward-compat with the existing frontend; new fields are additive.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd


# ── Session windows in America/New_York local time ───────────────────────────
# Times are ET hour-of-day. Asian wraps midnight so we treat it as the union
# of [18:00, 24:00) U [00:00, 03:00).
NY_TZ = "America/New_York"
RTH_START_H = 9.5    # 09:30 ET
RTH_END_H   = 16.0   # 16:00 ET
ASIA_START_H = 18.0  # 18:00 ET prev day
ASIA_END_H   = 3.0   # 03:00 ET current day
LONDON_END_H = 9.5   # London ends at NY open


def _session_label(et_hour: float) -> str:
    """Return the current trading session given a fractional ET hour."""
    if et_hour >= ASIA_START_H or et_hour < ASIA_END_H:
        return "asian"
    if et_hour < LONDON_END_H:
        return "london"
    if et_hour < RTH_END_H:
        return "ny"
    return "overnight"


def _prior_rth_high_low_close(df_et: pd.DataFrame) -> Optional[tuple]:
    """Return (PDH, PDL, PDC) using yesterday's RTH session (09:30-16:00 ET).

    df_et is a 1m-resolution DataFrame indexed by ET-aware timestamps with
    open/high/low/close columns."""
    if df_et.empty:
        return None
    now_et = df_et.index[-1]
    today_et = now_et.normalize()
    # Walk back day-by-day until we find a day with RTH data (skips weekends/holidays)
    for back in range(1, 8):
        day = today_et - pd.Timedelta(days=back)
        # 09:30 → 16:00 ET on that day
        start = day + pd.Timedelta(hours=RTH_START_H)
        end   = day + pd.Timedelta(hours=RTH_END_H)
        win = df_et[(df_et.index >= start) & (df_et.index < end)]
        if len(win) >= 30:  # need a real RTH session, not a holiday early-close
            return (float(win["high"].max()),
                    float(win["low"].min()),
                    float(win["close"].iloc[-1]))
    return None


def _asian_range(df_et: pd.DataFrame) -> Optional[tuple]:
    """Return (asian_high, asian_low, in_progress: bool) for today's Asian
    session. Asian = 18:00 ET prev day through 03:00 ET today. If we're
    currently inside that window the range is in-progress (uses bars so far)."""
    if df_et.empty:
        return None
    now_et = df_et.index[-1]
    et_hour = now_et.hour + now_et.minute / 60.0
    today_et = now_et.normalize()

    # If we're already past 03:00 ET, the Asian session for "today's trading
    # day" started at 18:00 yesterday and ended at 03:00 today.
    if et_hour >= ASIA_END_H:
        start = today_et - pd.Timedelta(days=1) + pd.Timedelta(hours=ASIA_START_H)
        end   = today_et + pd.Timedelta(hours=ASIA_END_H)
        in_progress = False
    else:
        # We're inside the Asian session (before 03:00 ET). It started at 18:00
        # ET yesterday and is still running.
        start = today_et - pd.Timedelta(days=1) + pd.Timedelta(hours=ASIA_START_H)
        end   = now_et
        in_progress = True

    win = df_et[(df_et.index >= start) & (df_et.index <= end)]
    if len(win) < 5:
        return None
    return float(win["high"].max()), float(win["low"].min()), in_progress


def _opening_type(df_et: pd.DataFrame, pdh: float, pdl: float) -> str:
    """Classify today's RTH open vs prior day's range.

    Returns gap_up / gap_down / inside / pending (if we haven't hit 09:30 ET
    yet today, we use the latest available price as the proxy)."""
    if df_et.empty:
        return "pending"
    now_et = df_et.index[-1]
    today_et = now_et.normalize()
    rth_open_ts = today_et + pd.Timedelta(hours=RTH_START_H)

    if now_et < rth_open_ts:
        # Pre-market: use current price as a "would-be open" proxy
        proxy = float(df_et["close"].iloc[-1])
    else:
        # Find the 09:30 ET print (closest bar at/after 09:30)
        post = df_et[df_et.index >= rth_open_ts]
        if post.empty:
            return "pending"
        proxy = float(post["open"].iloc[0])

    if proxy > pdh:
        return "gap_up"
    if proxy < pdl:
        return "gap_down"
    return "inside"


def _sweep_flags(df_et: pd.DataFrame, pdh: float, pdl: float,
                  asian_high: Optional[float], asian_low: Optional[float]) -> dict:
    """Check whether today's trading day has poked through PDH/PDL and/or
    the Asian high/low. "Today's trading day" starts at 18:00 ET yesterday."""
    if df_et.empty:
        return {"pdh_swept": False, "pdl_swept": False,
                "asian_swept_high": False, "asian_swept_low": False}
    now_et = df_et.index[-1]
    today_et = now_et.normalize()
    et_hour = now_et.hour + now_et.minute / 60.0
    # Trading-day start: 18:00 ET previous day (or two days back if it's currently before 18:00)
    if et_hour >= ASIA_START_H:
        td_start = today_et + pd.Timedelta(hours=ASIA_START_H)
    else:
        td_start = today_et - pd.Timedelta(days=1) + pd.Timedelta(hours=ASIA_START_H)
    win = df_et[df_et.index >= td_start]
    if win.empty:
        return {"pdh_swept": False, "pdl_swept": False,
                "asian_swept_high": False, "asian_swept_low": False}
    hi = float(win["high"].max())
    lo = float(win["low"].min())
    return {
        "pdh_swept": hi > pdh,
        "pdl_swept": lo < pdl,
        "asian_swept_high": (asian_high is not None) and hi > asian_high,
        "asian_swept_low":  (asian_low  is not None) and lo < asian_low,
    }


def _draw_on_liquidity(price: float, trend: str,
                        pdh: float, pdl: float,
                        asian_high: Optional[float], asian_low: Optional[float]) -> Optional[dict]:
    """Pick the most-relevant unmitigated liquidity level price is being
    drawn toward. Heuristic: the nearest still-intact level in the direction
    of the longer-term trend wins. If trend is neutral, pick whichever level
    is closer."""
    candidates = []
    if pdh is not None and price < pdh:
        candidates.append({"label": "PDH", "level": pdh, "side": "above"})
    if pdl is not None and price > pdl:
        candidates.append({"label": "PDL", "level": pdl, "side": "below"})
    if asian_high is not None and price < asian_high:
        candidates.append({"label": "Asian high", "level": asian_high, "side": "above"})
    if asian_low is not None and price > asian_low:
        candidates.append({"label": "Asian low", "level": asian_low, "side": "below"})
    if not candidates:
        return None

    # Prefer trend direction
    if trend.endswith("bullish"):
        ups = [c for c in candidates if c["side"] == "above"]
        if ups:
            return min(ups, key=lambda c: abs(c["level"] - price))
    if trend.endswith("bearish"):
        dns = [c for c in candidates if c["side"] == "below"]
        if dns:
            return min(dns, key=lambda c: abs(c["level"] - price))
    return min(candidates, key=lambda c: abs(c["level"] - price))


def _intraday_bias(trend: str, position_vs_pd: str, opening_type: str,
                    sweeps: dict, price: float, pdh: float, pdl: float,
                    asian_high: Optional[float], asian_low: Optional[float]) -> str:
    """Synthesise the headline bias from 30D trend + intraday state.

    Rules:
      • Trend bullish + Asian swept PDL + price reclaimed back above PDL
        = bullish (textbook reversal long setup)
      • Trend bullish + price broke and held above PDH = strong_bullish
      • Trend bullish + price under PDL with no reclaim = neutral (cautious)
      • Mirror for bearish.
      • Neutral trend follows intraday structure only.
    """
    above_pdl = price > pdl if pdl is not None else True
    below_pdh = price < pdh if pdh is not None else True

    if trend.endswith("bullish"):
        if sweeps["pdh_swept"] and price >= pdh:
            return "strong_bullish"
        if sweeps["asian_swept_low"] and price >= (asian_low or price):
            return "bullish"
        if sweeps["pdl_swept"] and not above_pdl:
            return "neutral"   # trend up but currently broken below — wait
        return "bullish"

    if trend.endswith("bearish"):
        if sweeps["pdl_swept"] and price <= pdl:
            return "strong_bearish"
        if sweeps["asian_swept_high"] and price <= (asian_high or price):
            return "bearish"
        if sweeps["pdh_swept"] and not below_pdh:
            return "neutral"
        return "bearish"

    # Neutral trend
    if sweeps["pdh_swept"] and price >= pdh:
        return "bullish"
    if sweeps["pdl_swept"] and price <= pdl:
        return "bearish"
    return "neutral"


def _narrative(instrument: str, trend: str, intraday_bias: str,
                session: str, sweeps: dict, draw: Optional[dict],
                position_vs_pd: str, opening_type: str) -> str:
    """Plain-English summary the user can read in 2 seconds."""
    trend_phrase = {
        "strong_bullish": "Strong 30-day bullish trend",
        "bullish":        "30-day trend up",
        "neutral":        "30-day trend flat",
        "bearish":        "30-day trend down",
        "strong_bearish": "Strong 30-day bearish trend",
    }.get(trend, "Trend unclear")

    session_phrase = {
        "asian":     "Asian session in progress",
        "london":    "London session in progress",
        "ny":        "NY session in progress",
        "overnight": "Between sessions",
    }.get(session, "")

    sweep_bits = []
    if sweeps.get("pdh_swept"):       sweep_bits.append("swept PDH")
    if sweeps.get("pdl_swept"):       sweep_bits.append("swept PDL")
    if sweeps.get("asian_swept_high"): sweep_bits.append("swept Asian high")
    if sweeps.get("asian_swept_low"):  sweep_bits.append("swept Asian low")
    sweep_phrase = ("today " + " · ".join(sweep_bits)) if sweep_bits else "no liquidity sweeps yet"

    draw_phrase = ""
    if draw:
        draw_phrase = f"draw on liquidity → {draw['label']} ({draw['level']:.2f}) {draw['side']}"

    parts = [trend_phrase, session_phrase, sweep_phrase]
    if draw_phrase:
        parts.append(draw_phrase)

    return " · ".join(p for p in parts if p)


def compute_ict_bias(rows: list[tuple], instrument: str) -> dict:
    """Main entry point. `rows` is a list of (timestamp, open, high, low,
    close, volume) tuples in chronological order (60 days of 1m bars)."""
    base = {
        "instrument": instrument, "bias": "neutral", "strength_pct": 0.0,
        "last_close": None, "ema_fast": None, "ema_slow": None,
        "as_of": None, "trend": "neutral", "trend_strength_pct": 0.0,
        "pdh": None, "pdl": None, "pdc": None,
        "position_vs_pd": "unknown", "opening_type": "unknown",
        "asian_high": None, "asian_low": None,
        "pdh_swept": False, "pdl_swept": False,
        "asian_swept_high": False, "asian_swept_low": False,
        "current_session": "unknown",
        "draw_target": None,
        "narrative": "Not enough data yet.",
    }

    if not rows or len(rows) < 60:
        return base

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    df_et = df.tz_convert(NY_TZ)

    # 30-day daily EMA trend (preserved from the old logic)
    daily = df["close"].resample("1D").last().dropna()
    if len(daily) < 21:
        return base
    fast = daily.ewm(span=9, adjust=False).mean()
    slow = daily.ewm(span=21, adjust=False).mean()
    fast_now, slow_now = float(fast.iloc[-1]), float(slow.iloc[-1])
    spread_pct = ((fast_now - slow_now) / slow_now * 100.0) if slow_now else 0.0

    if   spread_pct >=  1.5: trend = "strong_bullish"
    elif spread_pct >=  0.3: trend = "bullish"
    elif spread_pct <= -1.5: trend = "strong_bearish"
    elif spread_pct <= -0.3: trend = "bearish"
    else:                    trend = "neutral"

    # Prior-day RTH levels
    prior = _prior_rth_high_low_close(df_et)
    pdh = pdl = pdc = None
    if prior:
        pdh, pdl, pdc = prior

    # Asian session range
    asian = _asian_range(df_et)
    asian_high = asian_low = None
    if asian:
        asian_high, asian_low, _ = asian

    # Current session — from the WALL CLOCK, never the last bar: a lagging or
    # delayed feed put the label a session behind (2026-07-13: 9:31 ET showed
    # "london" because the newest bar predated the 9:30 boundary).
    from datetime import datetime as _dt_wall
    from zoneinfo import ZoneInfo as _ZI
    _now_wall = _dt_wall.now(_ZI("America/New_York"))
    if _now_wall.weekday() >= 5 and not (_now_wall.weekday() == 6 and _now_wall.hour >= 18):
        session = "overnight"  # weekend (Sunday reopens 18:00 ET as asian)
    else:
        session = _session_label(_now_wall.hour + _now_wall.minute / 60.0)
    last_close = float(df_et["close"].iloc[-1])

    # Opening type
    if pdh is not None and pdl is not None:
        opening_type = _opening_type(df_et, pdh, pdl)
    else:
        opening_type = "unknown"

    # Position vs prior day
    if pdh is not None and pdl is not None:
        if last_close > pdh:
            position_vs_pd = "above_pdh"
        elif last_close < pdl:
            position_vs_pd = "below_pdl"
        else:
            position_vs_pd = "inside"
    else:
        position_vs_pd = "unknown"

    sweeps = _sweep_flags(df_et, pdh or 0, pdl or 0, asian_high, asian_low)

    draw = None
    if pdh is not None and pdl is not None:
        draw = _draw_on_liquidity(last_close, trend, pdh, pdl, asian_high, asian_low)

    # Intraday-aware headline (replaces the old "bias" semantics)
    if pdh is not None and pdl is not None:
        intraday_bias = _intraday_bias(trend, position_vs_pd, opening_type,
                                        sweeps, last_close, pdh, pdl,
                                        asian_high, asian_low)
    else:
        intraday_bias = trend  # fall back to 30D if we can't read levels

    narrative = _narrative(instrument, trend, intraday_bias, session,
                            sweeps, draw, position_vs_pd, opening_type)

    return {
        "instrument": instrument,
        # Headline — now intraday-aware
        "bias": intraday_bias,
        "strength_pct": round(spread_pct, 2),  # kept for backward-compat (== trend spread)
        "last_close": round(last_close, 2),
        "ema_fast": round(fast_now, 2),
        "ema_slow": round(slow_now, 2),
        "as_of": df_et.index[-1].isoformat(),

        # New ICT fields
        "trend": trend,
        "trend_strength_pct": round(spread_pct, 2),
        "pdh": round(pdh, 2) if pdh is not None else None,
        "pdl": round(pdl, 2) if pdl is not None else None,
        "pdc": round(pdc, 2) if pdc is not None else None,
        "position_vs_pd": position_vs_pd,
        "opening_type": opening_type,
        "asian_high": round(asian_high, 2) if asian_high is not None else None,
        "asian_low":  round(asian_low,  2) if asian_low  is not None else None,
        "pdh_swept": sweeps["pdh_swept"],
        "pdl_swept": sweeps["pdl_swept"],
        "asian_swept_high": sweeps["asian_swept_high"],
        "asian_swept_low":  sweeps["asian_swept_low"],
        "current_session": session,
        "draw_target": draw,
        "narrative": narrative,
    }
