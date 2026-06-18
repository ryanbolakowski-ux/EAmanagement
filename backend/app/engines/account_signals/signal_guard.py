"""Signal validation + idempotency helpers for account/email signals.

Pure functions (no DB / no network) so they are trivially unit-testable.

- validate_geometry: enforces long/short stop-entry-TP ordering, computes
  risk / reward / RR, and flags unrealistically tight stops per instrument.
- make_idempotency_key: stable hash of the signal's identifying fields so a
  scanner that re-fires the same setup on consecutive bars maps to ONE key.
"""
from __future__ import annotations
import hashlib
from typing import Optional


# Minimum realistic stop distance (in points) per instrument family. Stops
# tighter than this are flagged — on ES a 0.5pt stop is almost always noise /
# a bad signal, not a real structural stop.
MIN_STOP_POINTS = {
    "ES": 2.0, "MES": 2.0,
    "NQ": 8.0, "MNQ": 8.0,
    "YM": 20.0, "MYM": 20.0,
    "RTY": 2.0, "M2K": 2.0,
}
DEFAULT_MIN_STOP_POINTS = 1.0


def _round(x: float, n: int = 4) -> float:
    return round(float(x), n)


def validate_geometry(direction: str, entry: float, stop: float,
                      take_profit: float, instrument: str = "") -> dict:
    """Return a dict describing the signal's validity + risk geometry.

    Keys: valid (bool), error (str|None), warnings (list[str]),
          risk, reward, rr (floats), direction (normalized).
    """
    warnings: list[str] = []
    d = (direction or "").lower().strip()
    if d in ("buy", "long"):
        d = "long"
    elif d in ("sell", "short"):
        d = "short"
    else:
        return {"valid": False, "error": f"unknown direction {direction!r}",
                "warnings": warnings, "risk": 0.0, "reward": 0.0, "rr": 0.0,
                "direction": direction}

    try:
        entry = float(entry); stop = float(stop); take_profit = float(take_profit)
    except (TypeError, ValueError):
        return {"valid": False, "error": "non-numeric price", "warnings": warnings,
                "risk": 0.0, "reward": 0.0, "rr": 0.0, "direction": d}

    # Hard geometry rules
    if d == "long":
        if not (stop < entry < take_profit):
            return {"valid": False,
                    "error": f"long requires stop<entry<take_profit, got {stop}/{entry}/{take_profit}",
                    "warnings": warnings, "risk": 0.0, "reward": 0.0, "rr": 0.0, "direction": d}
    else:  # short
        if not (take_profit < entry < stop):
            return {"valid": False,
                    "error": f"short requires take_profit<entry<stop, got {take_profit}/{entry}/{stop}",
                    "warnings": warnings, "risk": 0.0, "reward": 0.0, "rr": 0.0, "direction": d}

    risk = abs(entry - stop)
    reward = abs(take_profit - entry)
    rr = (reward / risk) if risk > 0 else 0.0
    if risk <= 0:
        return {"valid": False, "error": "zero risk (entry==stop)", "warnings": warnings,
                "risk": 0.0, "reward": reward, "rr": 0.0, "direction": d}

    # TINY-RANGE-HARD-REJECT. For FUTURES (instrument in MIN_STOP_POINTS) a
    # sub-floor stop is a meaningless/noise bracket -> HARD REJECT (blocks the
    # email, the signal, and any routed paper/live entry). For non-futures the
    # floor is only a soft default, so a small dollar stop on a stock warns.
    _inst_u = (instrument or "").upper()
    min_stop = MIN_STOP_POINTS.get(_inst_u, DEFAULT_MIN_STOP_POINTS)
    if risk < min_stop:
        if _inst_u in MIN_STOP_POINTS:
            return {"valid": False,
                    "error": f"stop too tight: {risk:.2f}pt risk on {instrument} "
                             f"< {min_stop:.2f}pt floor (meaningless/noise range)",
                    "warnings": warnings, "risk": _round(risk),
                    "reward": _round(reward), "rr": _round(rr, 2), "direction": d}
        warnings.append(
            f"stop is very tight: {risk:.2f}pt risk on {instrument or '?'} "
            f"(min realistic ~{min_stop:.2f}pt) — likely noise"
        )
    if rr < 1.0:
        warnings.append(f"reward:risk is below 1.0 (rr={rr:.2f})")

    return {"valid": True, "error": None, "warnings": warnings,
            "risk": _round(risk), "reward": _round(reward), "rr": _round(rr, 2),
            "direction": d}


# Tick size per instrument family — used to quantize entry/stop/tp into
# bands so two signals on the same setup ~1 tick apart collapse to ONE key.
# Without this the watcher would generate a fresh key every minute when the
# next bar's micro-price-drift shifts entry/stop by 0.1pt.
TICK_SIZES_KEY = {
    "ES": 0.25, "NQ": 0.25, "RTY": 0.10, "YM": 1.0,
    "MES": 0.25, "MNQ": 0.25, "M2K": 0.10, "MYM": 1.0,
}


def _tick_band(price: float, tick: float = 0.25) -> str:
    """Round to nearest tick band so prices within 1 tick → same key.

    Example (tick=0.25): 7540.13 → 7540.25, 7540.31 → 7540.25, 7540.49 → 7540.50.
    Two signals where every price differs by < 1 tick produce identical bands.
    """
    if tick <= 0:
        tick = 0.25
    banded = round(float(price) / tick) * tick
    return f"{banded:.2f}"


def make_idempotency_key(watcher_id, strategy_id, instrument: str, direction: str,
                         bar_ts, entry: float, stop: float, take_profit: float) -> str:
    """Stable content hash. Same SETUP SHAPE -> same key, no matter how many
    times the scanner re-detects it on consecutive bars.

    The key intentionally OMITS the bar timestamp and quantizes prices into
    tick bands. Previous version included `bar_ts` (whole-second ISO) and
    2dp prices, so a setup that stayed valid for 60 bars produced 60 distinct
    keys → dedup window was bypassed → user got 60 signals/hour.

    Cooldown is the dedup mechanism (handled by the caller's query), not
    timestamp variance.
    """
    d = (direction or "").lower()
    if d in ("buy",): d = "long"
    if d in ("sell",): d = "short"
    inst = (instrument or "").upper()
    tick = TICK_SIZES_KEY.get(inst, 0.25)
    parts = [
        str(watcher_id), str(strategy_id), inst, d,
        _tick_band(entry, tick),
        _tick_band(stop, tick),
        _tick_band(take_profit, tick),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()
