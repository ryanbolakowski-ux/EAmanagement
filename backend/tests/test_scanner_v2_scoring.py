"""Scanner V2 scoring — monotonicity + never-fabricate guarantees.

The forensics-grounded invariants (docs/v2/01-scanner-forensics.md):
  * higher rel_vol -> higher score (the one measured positive signal)
  * gap 40% scores BELOW gap 10% all else equal (big gaps mean-revert)
  * missing candidate/context fields -> neutral contribution, never a crash

Run: pytest backend/tests/test_scanner_v2_scoring.py -v -p no:cacheprovider
"""
from __future__ import annotations

import pytest

from app.engines.scanner.v2.scoring import (
    WEIGHTS, ScoreBreakdownV2, score_v2, rel_vol_raw, gap_quality_raw, price_band_raw,
)

# A liquid mid-quality candidate — tests vary ONE feature off this base.
BASE = {
    "ticker": "TEST", "price": 6.0, "gap_pct": 8.0, "rel_vol": 10.0,
    "today_vol": 5_000_000, "dollar_vol": 30_000_000,
}


# ── rel_vol: dominant + monotone ────────────────────────────────────────────

def test_higher_rel_vol_scores_higher():
    lo = score_v2({**BASE, "rel_vol": 5.0}, {})
    hi = score_v2({**BASE, "rel_vol": 50.0}, {})
    assert hi.total > lo.total


def test_rel_vol_monotone_across_curve():
    prev = -1.0
    for rv in (1.0, 2.5, 5.0, 10.0, 25.0, 57.5, 100.0):
        raw = rel_vol_raw(rv)
        assert raw >= prev
        prev = raw
    assert rel_vol_raw(1.0) == 0.0
    assert rel_vol_raw(100.0) == 1.0
    # log scaling: a 500x print must not blow past the cap
    assert rel_vol_raw(500.0) == 1.0


def test_rel_vol_is_the_dominant_weight():
    assert WEIGHTS["rel_vol"] == max(WEIGHTS.values())
    assert WEIGHTS["rel_vol"] >= 0.35 * sum(WEIGHTS.values())


# ── gap_quality: capped penalty curve, not a linear bonus ───────────────────

def test_gap_40_scores_below_gap_10():
    g10 = score_v2({**BASE, "gap_pct": 10.0}, {})
    g40 = score_v2({**BASE, "gap_pct": 40.0}, {})
    assert g40.total < g10.total


def test_gap_curve_shape():
    # optimal band 3-15%, decay above 20%, strong penalty >30%
    assert gap_quality_raw(8.0) == pytest.approx(1.0)
    assert gap_quality_raw(25.0) < gap_quality_raw(15.0)
    assert gap_quality_raw(35.0) < 0.2
    assert gap_quality_raw(40.0) < gap_quality_raw(20.0) < gap_quality_raw(10.0)


# ── price_band: sub-$3 penalized, no hard floor ─────────────────────────────

def test_sub_3_dollar_penalized_but_not_rejected():
    cheap = score_v2({**BASE, "price": 1.50}, {})
    mid = score_v2({**BASE, "price": 6.0}, {})
    assert cheap.total < mid.total
    assert cheap.total > 0  # penalty, not a hard reject


def test_price_band_edge_zone_beats_large_caps():
    # under-$10 measured 35% WR +1.76 avg; over-$30 measured 0W-4L
    assert price_band_raw(6.0) > price_band_raw(45.0)


# ── never fabricate: missing data -> neutral, noted, no crash ───────────────

def test_missing_context_is_neutral_not_crash():
    bd = score_v2(dict(BASE), {})
    assert isinstance(bd, ScoreBreakdownV2)
    assert 0.0 <= bd.total <= 100.0
    assert bd.components["rs_vs_qqq"]["raw"] == 0.5
    assert bd.components["rs_vs_qqq"]["neutral"] is True
    assert bd.components["regime"]["raw"] == 0.5
    assert bd.components["regime"]["neutral"] is True
    why = bd.why()
    assert isinstance(why, str)
    assert "neutral" in why.lower()


def test_empty_candidate_and_context_do_not_crash():
    bd = score_v2({}, {})
    assert 0.0 <= bd.total <= 100.0
    # every component present, all flagged neutral except the hard-zero ones
    assert set(bd.components) == set(WEIGHTS)


def test_missing_dollar_vol_neutral_liquidity():
    c = {k: v for k, v in BASE.items() if k != "dollar_vol"}
    bd = score_v2(c, {})
    assert bd.components["liquidity_quality"]["neutral"] is True
    assert bd.components["liquidity_quality"]["raw"] == 0.5


# ── context features move the score the right way when PRESENT ──────────────

def test_rs_vs_qqq_direction():
    ctx = {"qqq_day_pct": 1.0}
    strong = score_v2({**BASE, "day_pct": 12.0}, ctx)
    weak = score_v2({**BASE, "day_pct": -2.0}, ctx)
    assert strong.total > weak.total


def test_regime_flag_direction():
    up = score_v2(dict(BASE), {"qqq_above_prev_close": True})
    dn = score_v2(dict(BASE), {"qqq_above_prev_close": False})
    assert up.total > dn.total


def test_catalyst_passthrough():
    with_cat = score_v2({**BASE, "catalyst_weight": 1.5, "catalyst_reason": "8-K: FDA approval"}, {})
    without = score_v2({**BASE, "catalyst_weight": 1.0}, {})
    assert with_cat.total > without.total
    assert "FDA" in with_cat.components["catalyst"]["note"]
    assert "FDA" in with_cat.why(k=len(with_cat.components))  # full breakdown carries the reason


def test_premarket_dollar_vol_lifts_liquidity():
    liquid = score_v2({**BASE, "premarket_dollar_vol": 2_000_000.0}, {})
    thin = score_v2({**BASE, "premarket_dollar_vol": 100_000.0}, {})
    assert liquid.total > thin.total


# ── scale sanity ────────────────────────────────────────────────────────────

def test_total_bounded_0_100_extremes():
    monster = score_v2({"ticker": "X", "price": 7.0, "gap_pct": 9.0, "rel_vol": 500.0,
                        "dollar_vol": 500_000_000, "premarket_dollar_vol": 50_000_000,
                        "catalyst_weight": 3.0, "day_pct": 30.0},
                       {"qqq_day_pct": 0.1, "qqq_above_prev_close": True})
    dud = score_v2({"ticker": "Y", "price": 0.50, "gap_pct": 60.0, "rel_vol": 1.0,
                    "dollar_vol": 0, "premarket_dollar_vol": 0, "catalyst_weight": 1.0,
                    "day_pct": -5.0},
                   {"qqq_day_pct": 2.0, "qqq_above_prev_close": False})
    assert 0.0 <= dud.total < monster.total <= 100.0


def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == pytest.approx(100.0)
