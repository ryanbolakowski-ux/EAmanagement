"""Canonical 0-100 candidate scoring (SCANNER-V1, P2).

Fuses the available signals into one rank with a transparent per-component
breakdown (used in logs + the "why selected" payload). Components default to a
NEUTRAL 0.5 contribution when their data isn't available yet — e.g. `trend`
needs daily history (the equity_daily backfill) and `strategy_perf` needs >=30
real measured outcomes — so nothing is ever fabricated. Weights are per-template
(a template can zero a component it doesn't use). Score is normalized to 0-100
over whatever components are active.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScoreBreakdown:
    total: float
    components: dict  # name -> {"raw": float, "weighted": float, "note": str}

    def why(self, k: int = 4) -> list:
        ranked = sorted(self.components.items(), key=lambda kv: kv[1]["weighted"], reverse=True)
        return [f"{n} ({v['note']})" for n, v in ranked[:k] if v["weighted"] > 0]


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


DEFAULT_WEIGHTS = {
    "rel_vol": 1.5, "premarket_dollar_vol": 1.0, "liquidity": 1.0, "momentum": 1.2,
    "catalyst": 1.0, "atr_fit": 0.8, "rr": 1.2, "vwap": 1.0, "breakout_quality": 1.2,
    "trend": 1.0, "strategy_perf": 1.0, "options_liquidity": 0.5,
}


def score_candidate(features: dict, weights: dict | None = None,
                    atr_min_pct: float = 2.0, atr_max_pct: float = 12.0) -> ScoreBreakdown:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    comp: dict = {}

    def add(name: str, raw01: float, note: str):
        wt = float(w.get(name, 0.0))
        comp[name] = {"raw": round(raw01, 3), "weighted": round(raw01 * wt, 3), "note": note}

    rv = float(features.get("rel_vol") or 0)
    add("rel_vol", _clip((rv - 1.0) / 9.0), f"{rv:.1f}x")

    pmv = features.get("premarket_dollar_vol")
    if pmv is None:
        add("premarket_dollar_vol", 0.5, "n/a (neutral)")
    else:
        add("premarket_dollar_vol", _clip(float(pmv) / 20_000_000.0), f"${float(pmv)/1e6:.1f}M pre")

    dv = float(features.get("dollar_vol") or 0)
    add("liquidity", _clip(dv / 50_000_000.0), f"${dv/1e6:.0f}M $-vol")

    g = abs(float(features.get("gap_pct") or 0))
    add("momentum", _clip(g / 20.0), f"gap {g:.1f}%")

    cw = float(features.get("catalyst_weight") or 1.0)
    add("catalyst", _clip(0.4 + (cw - 1.0) * 0.6) if cw > 1.0 else 0.4,
        features.get("catalyst_reason") or "none")

    atrp = features.get("atr_pct")
    if atrp is None:
        add("atr_fit", 0.5, "n/a")
    else:
        atrp = float(atrp)
        inband = atr_min_pct <= atrp <= atr_max_pct
        add("atr_fit", 1.0 if inband else _clip(1.0 - abs(atrp - (atr_min_pct + atr_max_pct) / 2) / max(atr_max_pct, 1)),
            f"{atrp:.1f}%" + ("" if inband else " (out of band)"))

    rr = features.get("rr")
    add("rr", 0.5 if rr is None else _clip(float(rr) / 3.0), "n/a" if rr is None else f"{float(rr):.1f}R")

    av = features.get("above_vwap")
    add("vwap", 0.5 if av is None else float(bool(av)),
        "n/a" if av is None else ("above VWAP" if av else "below VWAP"))

    bq = features.get("breakout_quality")
    add("breakout_quality", 0.5 if bq is None else _clip(float(bq)), features.get("breakout_note") or "n/a")

    tr = features.get("trend")
    add("trend", 0.5 if tr is None else _clip(float(tr)),
        "neutral (no daily history)" if tr is None else "trend")

    sp = features.get("strategy_perf")
    add("strategy_perf", 0.5 if sp is None else _clip(float(sp)),
        f"neutral (sample {int(features.get('perf_n', 0))}/30)" if sp is None else "measured edge")

    ol = features.get("options_liquidity")
    add("options_liquidity", 0.0 if ol is None else _clip(float(ol)), "n/a" if ol is None else "opts")

    max_pts = sum(float(w.get(n, 0.0)) for n in comp)
    raw_pts = sum(v["weighted"] for v in comp.values())
    total = round(100.0 * raw_pts / max_pts, 1) if max_pts > 0 else 0.0
    return ScoreBreakdown(total, comp)
