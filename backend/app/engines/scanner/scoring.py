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

import os
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
    # SARO-RANK-UPGRADE (2026-07-06 GPC-vs-IREN forensics): these two are only
    # ACTIVATED when the env gate is on (see score_candidate) so flag-off
    # scoring stays byte-identical.
    "analyst": 0.8, "fade_guard": 1.0,
}


def _rank_upgrade_enabled() -> bool:
    """SARO_RANK_UPGRADE env gate (default ON). Read at call time."""
    return os.environ.get("SARO_RANK_UPGRADE", "1") == "1"


def _trend_from_enrichment(f: dict):
    """(raw01, note) from enriched daily history, or (None, '') when the
    enrichment keys are absent / unusable (caller keeps the legacy neutral).

    Shapes (2026-07-06 forensics):
      * oversold reclaim  — multi-day capitulation (<=-15%/5d) gapping up >=5%
        (IREN: 5d -18.7%, Monday gap +8.11%, +13% on the day)     -> 0.9
      * blowoff fade risk — 20d run >30% + prior-day pop >8% + gap DOWN
        (GPC: 20d +34.9%, prior +12.9%, real Monday gap -2.5%)    -> 0.1
      * healthy uptrend   — 0 < 20d <= 25% with the gap in trend direction -> 0.85
    """
    try:
        chg5 = f.get("chg_5d_pct")
        chg20 = f.get("chg_20d_pct")
        if chg5 is None and chg20 is None:
            return None, ""
        gap = f.get("live_gap_pct")
        if gap is None:
            gap = f.get("gap_pct")
        gap = float(gap) if gap is not None else None
        chg5 = float(chg5) if chg5 is not None else None
        chg20 = float(chg20) if chg20 is not None else None
        prior = f.get("prior_day_ret_pct")
        prior = float(prior) if prior is not None else None
        if chg5 is not None and gap is not None and chg5 <= -15.0 and gap >= 5.0:
            return 0.9, f"oversold reclaim ({chg5:+.0f}%/5d, gap {gap:+.1f}%)"
        if (chg20 is not None and chg20 > 30.0 and prior is not None
                and prior > 8.0 and gap is not None and gap <= 0.0):
            return 0.1, (f"blowoff fade risk (+{chg20:.0f}%/20d, "
                         f"prior {prior:+.0f}%, gap {gap:+.1f}%)")
        if chg20 is not None and 0.0 < chg20 <= 25.0 and gap is not None and gap > 0.0:
            return 0.85, f"healthy uptrend (+{chg20:.0f}%/20d)"
        return 0.5, f"trend neutral ({(chg20 if chg20 is not None else 0.0):+.0f}%/20d)"
    except Exception:
        return None, ""


def score_candidate(features: dict, weights: dict | None = None,
                    atr_min_pct: float = 2.0, atr_max_pct: float = 12.0) -> ScoreBreakdown:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    comp: dict = {}

    def add(name: str, raw01: float, note: str):
        wt = float(w.get(name, 0.0))
        comp[name] = {"raw": round(raw01, 3), "weighted": round(raw01 * wt, 3), "note": note}

    rv = float(features.get("rel_vol") or 0)
    rv_raw = _clip((rv - 1.0) / 9.0)
    rv_note = f"{rv:.1f}x"
    # SARO-RANK-UPGRADE: ADV20-denominated rel-vol can RESCUE a name whose
    # single-prev-day volume base was itself elevated (IREN failure mode 5).
    rva = features.get("rel_vol_adv20")
    if _rank_upgrade_enabled() and rva is not None:
        try:
            rva_raw = _clip((float(rva) - 0.3) / 1.2)
            if rva_raw > rv_raw:
                rv_raw, rv_note = rva_raw, f"{float(rva):.1f}x adv20"
        except Exception:
            pass
    add("rel_vol", rv_raw, rv_note)

    pmv = features.get("premarket_dollar_vol")
    if pmv is None:
        add("premarket_dollar_vol", 0.5, "n/a (neutral)")
    else:
        add("premarket_dollar_vol", _clip(float(pmv) / 20_000_000.0), f"${float(pmv)/1e6:.1f}M pre")

    dv = float(features.get("dollar_vol") or 0)
    liq_raw = _clip(dv / 50_000_000.0)
    liq_note = f"${dv/1e6:.0f}M $-vol"
    # SARO-RANK-UPGRADE: blend 50/50 with 20-day average dollar volume when
    # enriched (IREN adv20 $2.24B was 10x GPC's — the snapshot day-$vol hid it).
    adv = features.get("adv20_dollars")
    if _rank_upgrade_enabled() and adv is not None:
        try:
            liq_raw = 0.5 * liq_raw + 0.5 * _clip(float(adv) / 500_000_000.0)
            liq_note = f"${dv/1e6:.0f}M day / ${float(adv)/1e6:.0f}M adv20"
        except Exception:
            pass
    add("liquidity", liq_raw, liq_note)

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
    _trend_done = False
    if _rank_upgrade_enabled():
        _traw, _tnote = _trend_from_enrichment(features)
        if _traw is not None:
            add("trend", _clip(float(_traw)), _tnote)
            _trend_done = True
    if not _trend_done:
        add("trend", 0.5 if tr is None else _clip(float(tr)),
            "neutral (no daily history)" if tr is None else "trend")

    sp = features.get("strategy_perf")
    add("strategy_perf", 0.5 if sp is None else _clip(float(sp)),
        f"neutral (sample {int(features.get('perf_n', 0))}/30)" if sp is None else "measured edge")

    ol = features.get("options_liquidity")
    add("options_liquidity", 0.0 if ol is None else _clip(float(ol)), "n/a" if ol is None else "opts")

    # ── SARO-RANK-UPGRADE components (env-gated; OMITTED entirely when the
    # enrichment keys are absent, so un-enriched candidates score byte-identical
    # to the legacy formula — no drift vs the absolute 15/20 gate thresholds
    # used by the shadow/funnel callers that never enrich) ──
    if _rank_upgrade_enabled():
        up = features.get("analyst_upside_pct")
        if up is not None:
            try:
                up = float(up)
                a_raw = 0.1 if up <= 0 else _clip(up / 60.0)
                _rating = str(features.get("analyst_rating") or "").strip()
                if _rating.lower() in ("sell", "strong sell"):
                    a_raw = min(a_raw, 0.15)
                add("analyst", a_raw, f"{up:+.0f}% to consensus target"
                    + (f" ({_rating})" if _rating else ""))
            except Exception:
                pass  # unusable analyst data -> component omitted (no drift)

        prior = features.get("prior_day_ret_pct")
        if prior is not None:
            try:
                prior = float(prior)
                fg_gap = features.get("live_gap_pct")
                if fg_gap is None:
                    fg_gap = features.get("gap_pct")
                fg_gap = float(fg_gap) if fg_gap is not None else None
                if prior >= 8.0 and fg_gap is not None and fg_gap <= 0.0:
                    add("fade_guard", 0.0,
                        f"post-pop fade (prior {prior:+.0f}%, gap {fg_gap:+.1f}%)")
                else:
                    add("fade_guard", 1.0, "no fade shape")
            except Exception:
                pass  # unusable fade data -> component omitted (no drift)

    max_pts = sum(float(w.get(n, 0.0)) for n in comp)
    raw_pts = sum(v["weighted"] for v in comp.values())
    total = round(100.0 * raw_pts / max_pts, 1) if max_pts > 0 else 0.0
    return ScoreBreakdown(total, comp)
