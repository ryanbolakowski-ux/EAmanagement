"""
MARKET ACTIVITY GATE  (multi-factor)
====================================
GO / NO-GO switch for "is the tape worth trading right now?"
If GO -> deploy your strategies (alert + enter). If NO -> stand down.
It finds no trades itself; it's only the switch.

TIME IS NOT A GATE. The gate evaluates activity at EVERY hour. The only things
that produce a NO are no-movement conditions: dead volume, no range expansion,
or chop. A real move at 4am passes exactly like a 9:30 move. The session
"windows" are now just a small confluence bonus + a logging flag, never a block.

FACTORS (each scored 0..1, blended into one activity score)
  expansion     ATR now vs its recent median        (range woke up?)
  efficiency    Kaufman ER: net move / total path    (clean move vs chop)
  rel_volume    current volume vs average            (real participation?)
  speed         displacement per bar, in ATR units   (how fast it's moving)
  vwap          extension from session VWAP + slope  (trending vs pinned/balanced)
  rsi           |RSI-50| momentum push               (directional energy)
  liquidity     recent sweep of a swing/extreme      (stops being hunted = active)

HARD cutoffs (movement-based, not time-based): dead volume, volatility spike.
A directional HINT (long/short/neutral) is returned for routing, informational.

Candle = dict: {"ts": tz-aware ET datetime, "open","high","low","close","volume"}
Volume optional: if absent, volume & vwap factors drop and weights renormalize.
Feed CLOSED bars on the timeframe your strategies trigger on.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import time, datetime
from typing import Optional, Sequence
import statistics
import logging

# Stdlib logger so this module stays standalone-importable (no loguru dep).
_gate_log = logging.getLogger("theta.market_activity_gate")


# ==========================================================================
# CONFIG
# ==========================================================================

@dataclass
class GateConfig:
    # --- general ---
    atr_period: int = 14
    median_lookback: int = 50

    # --- per-factor thresholds (ramp lo->hi maps raw value to 0..1 score) ---
    expansion_lo: float = 0.8      # ATR/median: below lo -> dead
    expansion_hi: float = 1.6      # at/above hi -> fully "expanded"
    expansion_ceiling: float = 3.0 # HARD skip if ATR/median above this (0=off)

    efficiency_lookback: int = 20
    efficiency_lo: float = 0.20
    efficiency_hi: float = 0.55

    volume_lookback: int = 50
    relvol_lo: float = 0.8
    relvol_hi: float = 2.0
    volume_floor: float = 0.5      # HARD skip if relvol below this (dead tape)

    speed_lookback: int = 5
    speed_lo: float = 0.15         # ATRs of net travel per bar
    speed_hi: float = 0.70

    vwap_slope_lookback: int = 10
    vwap_ext_lo: float = 0.30      # |price-vwap| in ATR units
    vwap_ext_hi: float = 1.50

    rsi_period: int = 14
    rsi_push_lo: float = 8.0       # |rsi-50|
    rsi_push_hi: float = 25.0

    hunt_swing_lookback: int = 2   # fractal width for swing extremes
    hunt_recent_bars: int = 5      # a sweep within this many bars counts as "hunting"

    # --- factor weights (only available factors are used; auto-renormalized) ---
    weights: dict = field(default_factory=lambda: {
        "expansion": 1.0,
        "efficiency": 1.2,
        "rel_volume": 1.0,
        "speed": 1.0,
        "vwap": 0.8,
        "rsi": 0.6,
        "liquidity": 0.8,
    })

    go_threshold: float = 0.55     # blended score needed to deploy

    # --- "prime" windows (ET): the major opens, first ~90 min. ---
    #     NOT a gate. The gate trades ANY hour the tape is active. Being inside a
    #     window only adds `window_bonus` to the score (confluence) and sets
    #     result.in_window=True for logging. Set window_bonus=0 to ignore time
    #     entirely. NOTE: London/Asia drift +-1h across DST — verify seasonally.
    windows: tuple = (
        (time(19, 0), time(20, 30)),   # Asia / Tokyo open
        (time(2, 0),  time(4, 30)),    # London open
        (time(9, 30), time(11, 0)),    # NY open
    )
    window_bonus: float = 0.05     # score nudge when inside a prime window (0 = off)


# ==========================================================================
# RESULT TYPES
# ==========================================================================

@dataclass
class Factor:
    name: str
    score: float          # 0..1
    value: float          # raw reading
    detail: str = ""


@dataclass
class GateResult:
    go: bool
    score: float
    reason: str
    bias: str = "neutral"           # "long" | "short" | "neutral" (routing hint)
    in_window: bool = False         # was it inside a prime open? (info only)
    factors: list = field(default_factory=list)


# ==========================================================================
# SMALL HELPERS
# ==========================================================================

def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def _ramp(x: float, lo: float, hi: float) -> float:
    """Linear map: x<=lo -> 0, x>=hi -> 1."""
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return _clamp01((x - lo) / (hi - lo))


def _atr(c: Sequence[dict], period: int) -> Optional[float]:
    if len(c) < period + 1:
        return None
    trs = []
    for i in range(len(c) - period, len(c)):
        h, l, pc = c[i]["high"], c[i]["low"], c[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return statistics.fmean(trs)


def _has_volume(c: Sequence[dict]) -> bool:
    return all("volume" in x and x["volume"] is not None for x in c[-5:])


def _in_window(ts: datetime, windows) -> bool:
    if not windows:
        return False
    t = ts.time()
    for s, e in windows:
        if (s <= e and s <= t < e) or (s > e and (t >= s or t < e)):
            return True
    return False


# ==========================================================================
# FACTORS
# ==========================================================================

def f_expansion(c, cfg, atr_now):
    samples, start = [], max(cfg.atr_period + 1, len(c) - cfg.median_lookback)
    for end in range(start, len(c) + 1):
        a = _atr(c[:end], cfg.atr_period)
        if a is not None:
            samples.append(a)
    if atr_now is None or len(samples) < 5:
        return None
    med = statistics.median(samples)
    ratio = (atr_now / med) if med > 0 else 0.0
    return Factor("expansion", _ramp(ratio, cfg.expansion_lo, cfg.expansion_hi),
                  ratio, f"ATR {ratio:.2f}x median")


def f_efficiency(c, cfg):
    n = cfg.efficiency_lookback
    if len(c) < n + 1:
        return None
    w = c[-(n + 1):]
    net = abs(w[-1]["close"] - w[0]["close"])
    path = sum(abs(w[i]["close"] - w[i - 1]["close"]) for i in range(1, len(w)))
    er = (net / path) if path > 0 else 0.0
    return Factor("efficiency", _ramp(er, cfg.efficiency_lo, cfg.efficiency_hi),
                  er, f"ER {er:.2f}")


def f_rel_volume(c, cfg):
    if not _has_volume(c) or len(c) < cfg.volume_lookback:
        return None
    vols = [x["volume"] for x in c[-cfg.volume_lookback:]]
    med = statistics.median(vols[:-1]) if len(vols) > 1 else vols[-1]
    cur = c[-1]["volume"]
    relvol = (cur / med) if med > 0 else 0.0
    return Factor("rel_volume", _ramp(relvol, cfg.relvol_lo, cfg.relvol_hi),
                  relvol, f"{relvol:.2f}x avg vol")


def f_speed(c, cfg, atr_now):
    n = cfg.speed_lookback
    if atr_now is None or atr_now == 0 or len(c) < n + 1:
        return None
    net = abs(c[-1]["close"] - c[-(n + 1)]["close"])
    per_bar_atr = (net / n) / atr_now      # ATRs of net travel per bar
    return Factor("speed", _ramp(per_bar_atr, cfg.speed_lo, cfg.speed_hi),
                  per_bar_atr, f"{per_bar_atr:.2f} ATR/bar")


def _session_vwap(c: Sequence[dict]):
    """Running VWAP anchored to the start of the last candle's ET day."""
    if not _has_volume(c):
        return None
    day = c[-1]["ts"].date()
    cum_pv = cum_v = 0.0
    vwap = []
    for x in c:
        if x["ts"].date() != day:
            vwap.append(None)
            continue
        tp = (x["high"] + x["low"] + x["close"]) / 3
        cum_pv += tp * x["volume"]
        cum_v += x["volume"]
        vwap.append(cum_pv / cum_v if cum_v > 0 else None)
    return vwap


def f_vwap(c, cfg, atr_now):
    vwap = _session_vwap(c)
    if vwap is None or atr_now in (None, 0) or vwap[-1] is None:
        return None
    price = c[-1]["close"]
    ext = abs(price - vwap[-1]) / atr_now             # extension in ATR units
    k = cfg.vwap_slope_lookback
    prior = vwap[-k] if len(vwap) > k and vwap[-k] is not None else vwap[-1]
    slope = (vwap[-1] - prior)
    score = _ramp(ext, cfg.vwap_ext_lo, cfg.vwap_ext_hi)
    return Factor("vwap", score, ext,
                  f"{ext:.2f} ATR from VWAP, slope {slope:+.2f}")


def _rsi(c, period):
    if len(c) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(len(c) - period, len(c)):
        ch = c[i]["close"] - c[i - 1]["close"]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    if losses == 0:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100 - (100 / (1 + rs))


def f_rsi(c, cfg):
    r = _rsi(c, cfg.rsi_period)
    if r is None:
        return None
    push = abs(r - 50)
    return Factor("rsi", _ramp(push, cfg.rsi_push_lo, cfg.rsi_push_hi),
                  r, f"RSI {r:.0f}")


def f_liquidity(c, cfg):
    """
    'Stops being hunted' = a recent bar wicked beyond a prior swing/rolling
    extreme and then closed back inside = sweep + rejection. Presence within
    hunt_recent_bars -> active engineered movement.
    """
    if len(c) < cfg.median_lookback + cfg.hunt_recent_bars + 1:
        return None
    ref = c[:-cfg.hunt_recent_bars]
    ref_high = max(x["high"] for x in ref[-cfg.median_lookback:])
    ref_low = min(x["low"] for x in ref[-cfg.median_lookback:])
    hunted = False
    for x in c[-cfg.hunt_recent_bars:]:
        swept_high = x["high"] > ref_high and x["close"] < ref_high
        swept_low = x["low"] < ref_low and x["close"] > ref_low
        if swept_high or swept_low:
            hunted = True
            break
    return Factor("liquidity", 1.0 if hunted else 0.0,
                  1.0 if hunted else 0.0,
                  "sweep+reject seen" if hunted else "no recent hunt")


# ==========================================================================
# BIAS HINT  (informational routing)
# ==========================================================================

def _bias_hint(c, cfg, atr_now):
    score = 0
    if len(c) > cfg.speed_lookback:
        score += 1 if c[-1]["close"] > c[-(cfg.speed_lookback + 1)]["close"] else -1
    r = _rsi(c, cfg.rsi_period)
    if r is not None:
        score += 1 if r > 50 else -1
    vwap = _session_vwap(c)
    if vwap and vwap[-1] is not None:
        score += 1 if c[-1]["close"] > vwap[-1] else -1
    return "long" if score >= 2 else "short" if score <= -2 else "neutral"


# ==========================================================================
# THE GATE
# ==========================================================================

def market_gate(candles: Sequence[dict], cfg: GateConfig = GateConfig()) -> GateResult:
    if not candles:
        return GateResult(False, 0.0, "no data")

    in_window = _in_window(candles[-1]["ts"], cfg.windows)

    atr_now = _atr(candles, cfg.atr_period)
    if atr_now is None:
        return GateResult(False, 0.0, "not enough history for ATR", in_window=in_window)

    candidates = [
        f_expansion(candles, cfg, atr_now),
        f_efficiency(candles, cfg),
        f_rel_volume(candles, cfg),
        f_speed(candles, cfg, atr_now),
        f_vwap(candles, cfg, atr_now),
        f_rsi(candles, cfg),
        f_liquidity(candles, cfg),
    ]
    factors = [f for f in candidates if f is not None]
    if len(factors) < 3:
        return GateResult(False, 0.0, "not enough factors available yet",
                          in_window=in_window, factors=factors)

    # HARD cutoff: volatility spike (uncontrolled)
    exp = next((f for f in factors if f.name == "expansion"), None)
    if exp and cfg.expansion_ceiling and exp.value > cfg.expansion_ceiling:
        return GateResult(False, 0.0, f"too violent — ATR {exp.value:.2f}x median",
                          in_window=in_window, factors=factors)

    # HARD cutoff: dead volume
    rv = next((f for f in factors if f.name == "rel_volume"), None)
    if rv and rv.value < cfg.volume_floor:
        return GateResult(False, 0.0, f"dead — volume {rv.value:.2f}x avg",
                          in_window=in_window, factors=factors)

    # blended activity score (+ optional prime-window bonus)
    num = den = 0.0
    for f in factors:
        w = cfg.weights.get(f.name, 0.0)
        num += w * f.score
        den += w
    base = (num / den) if den > 0 else 0.0
    score = _clamp01(base + (cfg.window_bonus if in_window else 0.0))
    bias = _bias_hint(candles, cfg, atr_now)
    where = "prime window" if in_window else "off-hours"

    if score >= cfg.go_threshold:
        return GateResult(True, score, f"GO ({where}) — score {score:.2f}",
                          bias=bias, in_window=in_window, factors=factors)
    return GateResult(False, score,
                      f"stand down ({where}) — score {score:.2f} (< {cfg.go_threshold})",
                      bias=bias, in_window=in_window, factors=factors)


# ==========================================================================
# ENGINE ADAPTER — convert a pandas OHLC(V) bar buffer to the candle-dict
# contract market_gate() expects. (Added for integration; NOT part of the
# user's canonical gate logic — do not change the gate above.)
# ==========================================================================

def candles_from_df(df, tail: int = 200) -> list[dict]:
    """Convert a bar DataFrame (DatetimeIndex, columns open/high/low/close
    [/volume]) into the candle-dict list market_gate() expects, with tz-aware
    ET timestamps. Returns the last `tail` bars."""
    import pandas as _pd
    if df is None or len(df) == 0:
        return []
    d = df.tail(tail)
    idx = d.index
    if not isinstance(idx, _pd.DatetimeIndex):
        # A buffer that mixed tz-aware sources (e.g. a UTC cache seed + ET-aware
        # live bars) collapses to an object Index, not a DatetimeIndex. Coerce
        # to UTC rather than bailing to [] — bailing would silently ABSTAIN the
        # gate for the whole session (paper-trading mixed-tz buffer case).
        try:
            idx = _pd.to_datetime(idx, utc=True)
        except Exception:
            return []
    if idx.tz is None:
        idx = idx.tz_localize("UTC")     # naive assumed UTC (matches is_in_session)
    idx = idx.tz_convert("US/Eastern")
    out = []
    o = d["open"].to_numpy(); h = d["high"].to_numpy()
    lo = d["low"].to_numpy();  c = d["close"].to_numpy()
    has_vol = "volume" in d.columns
    v = d["volume"].to_numpy() if has_vol else None
    for i in range(len(d)):
        rec = {"ts": idx[i].to_pydatetime(), "open": float(o[i]), "high": float(h[i]),
               "low": float(lo[i]), "close": float(c[i])}
        if has_vol:
            rec["volume"] = float(v[i])
        out.append(rec)
    return out


# ==========================================================================
# FUTURES INTEGRATION HELPERS  (engine source-of-truth entry point)
# ==========================================================================
# These wrap market_gate() for the engine. The gate governs the FUTURES
# (Theta) path ONLY — email/paper/live all funnel through ICTStrategy.on_bar,
# so calling evaluate_activity_gate() there gives every route the SAME GO/NO-GO
# decision. (Added for integration; NOT part of the canonical gate logic.)

# Canonical index-futures roots traded on this platform. Matches the proxy ETF
# map + live tick tables (ES/NQ/RTY/YM + the micros). Add new roots here only.
FUTURES_ROOTS = {"ES", "NQ", "RTY", "YM", "MES", "MNQ", "M2K", "MYM"}

# market_gate() reasons that mean "couldn't judge the tape" (data availability)
# rather than "the tape is dead/chop". On these the gate ABSTAINS (no opinion),
# so a warmup / thin feed never masquerades as a chop stand-down.
_INSUFFICIENT_DATA_REASONS = (
    "no data", "not enough history", "not enough factors",
)


def _normalize_root(symbol: str) -> str:
    """Strip decoration to the bare futures root: '/MNQ', 'MNQ=F', 'MNQ1!',
    'C:MNQ1!', 'mnq' -> 'MNQ'."""
    s = (symbol or "").upper().strip()
    if ":" in s:                 # 'C:NQ1!' -> 'NQ1!'
        s = s.split(":", 1)[1]
    s = s.lstrip("/")
    if s.endswith("=F"):
        s = s[:-2]
    s = s.rstrip("!").rstrip("1") if s.endswith("1!") else s.rstrip("!")
    return s


def is_futures_symbol(symbol: str) -> bool:
    """True only for the index-futures roots the activity gate governs."""
    return _normalize_root(symbol) in FUTURES_ROOTS


def evaluate_activity_gate(instrument: str, df, cfg: "GateConfig | None" = None):
    """Source-of-truth futures GO/NO-GO for an OHLC(V) bar DataFrame.

    Returns:
      - None  -> ABSTAIN. The instrument isn't a gated future, or there isn't
                 enough data to judge the tape. Callers MUST treat None as
                 "no opinion, proceed" — NOT as a block (fail-open).
      - GateResult -> a real verdict; check `.go` (False = stand down).

    `cfg.go_threshold` may be overridden live via env FUTURES_GATE_GO_THRESHOLD
    (no redeploy needed) — leave unset to use the script default (0.55).
    """
    import os as _os
    if not is_futures_symbol(instrument):
        return None
    if cfg is None:
        cfg = GateConfig()
        # Calibrated FUTURES operating point (sweep on real NQ 1m / Futures
        # Signal Scanner, 2026-06): 0.40 on the 1m execution feed -> PF 1.56,
        # DD 1.9%, WR 54%. The script's stock default 0.55 was destructive here
        # (PF 1.05, ~$0 net). The canonical GateConfig.go_threshold (0.55) is
        # left untouched; we set the futures operating point at this boundary.
        # Env FUTURES_GATE_GO_THRESHOLD overrides for live tuning (no redeploy).
        cfg.go_threshold = 0.40
        _thr = _os.environ.get("FUTURES_GATE_GO_THRESHOLD")
        if _thr:
            try:
                _v = float(_thr)
                if 0.0 < _v < 1.0:
                    cfg.go_threshold = _v
                else:
                    # Reject the foot-guns: 0 -> always GO (neuters the chop
                    # stand-down); >=1 -> always NO (silent total stand-down).
                    _gate_log.warning(
                        "FUTURES_GATE_GO_THRESHOLD=%r out of (0,1) range; "
                        "ignoring, using %.2f", _thr, cfg.go_threshold)
            except (TypeError, ValueError):
                _gate_log.warning(
                    "FUTURES_GATE_GO_THRESHOLD=%r not parseable; using %.2f",
                    _thr, cfg.go_threshold)
    # Need enough bars for the multi-factor read (ATR + the longest lookback).
    need = cfg.atr_period + cfg.median_lookback + cfg.hunt_recent_bars + 2
    candles = candles_from_df(df, tail=max(need, 80))
    # Drop trailing FORMING bar(s): the live runner buffers the current,
    # not-yet-closed bar, which usually carries ~0 volume and would spuriously
    # trip the dead-volume hard cutoff (the gate is specified for CLOSED bars).
    while (len(candles) > 1
           and not candles[-1].get("volume")
           and any(c.get("volume") for c in candles[:-1])):
        candles = candles[:-1]
    if len(candles) < cfg.atr_period + 5:
        return None
    # If volume is still UNRELIABLE for this feed — the last bar is zero, or more
    # than half the recent window is zero (a stale/forming proxy buffer) — strip
    # volume entirely so the gate DROPS the volume + VWAP factors and judges on
    # price action (the documented "volume absent" path), instead of firing the
    # dead-volume hard cutoff on a data artifact. A genuinely dead market still
    # prints some volume on closed bars, so real chop is still caught by score.
    _recent = candles[-min(len(candles), cfg.volume_lookback):]
    _nz = sum(1 for c in _recent if c.get("volume"))
    if (not candles[-1].get("volume")) or _nz < max(5, len(_recent) // 2):
        candles = [{k: v for k, v in c.items() if k != "volume"} for c in candles]
    res = market_gate(candles, cfg)
    if (not res.go) and any(res.reason.startswith(p) for p in _INSUFFICIENT_DATA_REASONS):
        return None  # data availability, not a movement verdict -> abstain
    return res


# ==========================================================================
# USAGE
# ==========================================================================
#   g = market_gate(closed_bars, cfg)
#   if g.go:
#       send_alert_and_enter(direction_hint=g.bias)   # your existing layer
#   else:
#       stand_down()
#   for f in g.factors: print(f.name, round(f.score,2), f.detail)
#
#   # engine source-of-truth (futures only):
#   res = evaluate_activity_gate("MNQ", exec_tf_dataframe)
#   if res is not None and not res.go:
#       return None   # stand down — logged with res.reason
# ==========================================================================

if __name__ == "__main__":
    from datetime import timezone, timedelta
    ET = timezone(timedelta(hours=-5))

    def make(base, step, jitter, vol):
        out, p = [], 100.0
        for k in range(90):
            o = p
            c = o + step + (jitter if k % 2 else -jitter)
            out.append({"ts": base + timedelta(minutes=k), "open": o,
                        "high": max(o, c) + 0.05, "low": min(o, c) - 0.05,
                        "close": c, "volume": vol + (vol * 0.5 if k % 2 else 0)})
            p = c
        return out

    ny   = make(datetime(2026, 6, 17, 9, 30, tzinfo=ET), 0.15, 0.03, 1500)   # prime + moving
    odd  = make(datetime(2026, 6, 17, 13, 30, tzinfo=ET), 0.15, 0.03, 1500)  # off-hours + moving
    dead = make(datetime(2026, 6, 17, 13, 30, tzinfo=ET), 0.0,  0.20, 400)   # off-hours + chop

    for label, bars in (("NY open, moving", ny),
                        ("Off-hours 1:30pm, moving", odd),
                        ("Off-hours, chop", dead)):
        g = market_gate(bars)
        print(f"{label:26s} -> {'GO' if g.go else 'NO'} | in_window={g.in_window} "
              f"| {g.reason} | bias {g.bias}")
