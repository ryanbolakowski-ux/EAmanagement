"""SARO-RANK-UPGRADE unit tests (2026-07-06 GPC/IREN forensics). PURE — no
network, no DB: candidate dicts are constructed by hand and only the scoring
math + the pure stale-quote helper are exercised."""
import os

import pytest

from app.engines.options.theta_scanner import _stale_quote_check
from app.engines.scanner.scoring import score_candidate


@pytest.fixture(autouse=True)
def _gate_on():
    """Default every test to the upgrade being ON; restore afterwards."""
    prev = os.environ.get("SARO_RANK_UPGRADE")
    os.environ["SARO_RANK_UPGRADE"] = "1"
    yield
    if prev is None:
        os.environ.pop("SARO_RANK_UPGRADE", None)
    else:
        os.environ["SARO_RANK_UPGRADE"] = prev


def _base_candidate() -> dict:
    """A plain mid-pack momentum candidate with NO enrichment keys."""
    return {"ticker": "TEST", "price": 50.0, "gap_pct": 6.0, "rel_vol": 3.0,
            "today_vol": 1_200_000, "dollar_vol": 60_000_000}


# ── fade_guard ───────────────────────────────────────────────────────────────

def test_fade_guard_zeroes_on_gpc_shape():
    c = _base_candidate()
    c.update({"gap_pct": 12.92,           # stale snapshot gap
              "prior_day_ret_pct": 12.92,  # Thursday's pop
              "live_gap_pct": -4.84})      # real Monday tape
    sb = score_candidate(c)
    assert sb.components["fade_guard"]["raw"] == 0.0
    assert "fade" in sb.components["fade_guard"]["note"]


def test_fade_guard_full_when_no_fade_shape():
    c = _base_candidate()
    c.update({"prior_day_ret_pct": 3.0, "live_gap_pct": 4.0})
    sb = score_candidate(c)
    assert sb.components["fade_guard"]["raw"] == 1.0


def test_fade_guard_uses_snapshot_gap_when_no_live_gap():
    c = _base_candidate()
    c.update({"prior_day_ret_pct": 10.0, "gap_pct": -1.0})
    sb = score_candidate(c)
    assert sb.components["fade_guard"]["raw"] == 0.0


# ── trend ────────────────────────────────────────────────────────────────────

def test_trend_high_on_iren_oversold_reclaim_shape():
    c = _base_candidate()
    c.update({"chg_5d_pct": -18.7, "chg_20d_pct": -40.7,
              "live_gap_pct": 8.11, "prior_day_ret_pct": -10.39})
    sb = score_candidate(c)
    assert sb.components["trend"]["raw"] == 0.9
    assert "oversold reclaim" in sb.components["trend"]["note"]


def test_trend_blowoff_on_gpc_shape():
    c = _base_candidate()
    c.update({"chg_5d_pct": 18.0, "chg_20d_pct": 34.9,
              "prior_day_ret_pct": 12.92, "live_gap_pct": -2.54})
    sb = score_candidate(c)
    assert 0.0 <= sb.components["trend"]["raw"] <= 0.2


def test_trend_healthy_uptrend():
    c = _base_candidate()
    c.update({"chg_5d_pct": 4.0, "chg_20d_pct": 12.0, "live_gap_pct": 3.0})
    sb = score_candidate(c)
    assert sb.components["trend"]["raw"] >= 0.8


# ── analyst ──────────────────────────────────────────────────────────────────

def test_analyst_raw_clips():
    c = _base_candidate()
    c["analyst_upside_pct"] = 95.0  # IREN: consensus target +95% vs price
    assert score_candidate(c).components["analyst"]["raw"] == 1.0
    c["analyst_upside_pct"] = 8.2   # GPC: +8.2% (Hold)
    raw = score_candidate(c).components["analyst"]["raw"]
    assert abs(raw - 8.2 / 60.0) < 5e-4  # raw is rounded to 3 decimals in add()
    c["analyst_upside_pct"] = -5.0  # below consensus target
    assert score_candidate(c).components["analyst"]["raw"] == 0.1
    c.update({"analyst_upside_pct": 40.0, "analyst_rating": "Sell"})
    assert score_candidate(c).components["analyst"]["raw"] <= 0.15


# ── rel_vol adv20 rescue ─────────────────────────────────────────────────────

def test_rel_vol_adv20_rescues_elevated_prior_day_base():
    c = _base_candidate()
    c["rel_vol"] = 0.95  # IREN failure mode 5: prev-day volume base was itself elevated
    low = score_candidate(c).components["rel_vol"]["raw"]
    c["rel_vol_adv20"] = 1.8
    rescued = score_candidate(c).components["rel_vol"]["raw"]
    assert low == 0.0
    assert rescued == 1.0  # clip((1.8-0.3)/1.2) = 1.0


# ── neutrality + bounded drift when enrichment is missing ───────────────────

def test_missing_enrichment_keys_omitted_and_zero_drift():
    c = _base_candidate()  # no enrichment keys at all
    sb_new = score_candidate(c)
    # un-enriched candidates carry NO new components at all — byte-identical
    # to legacy, so the absolute 15/20 gates keep one scale everywhere
    assert "analyst" not in sb_new.components
    assert "fade_guard" not in sb_new.components
    assert sb_new.components["trend"]["raw"] == 0.5  # legacy neutral unchanged
    os.environ["SARO_RANK_UPGRADE"] = "0"
    try:
        sb_old = score_candidate(dict(c))
    finally:
        os.environ["SARO_RANK_UPGRADE"] = "1"
    assert abs(sb_new.total - sb_old.total) < 0.001


def test_zero_drift_at_score_extremes():
    # the old neutral-add pulled low scores up ~4 pts and high scores down ~4
    for cand in (
        {"ticker": "LOW", "price": 10.0, "gap_pct": 2.0, "rel_vol": 1.1,
         "today_vol": 200_000, "dollar_vol": 2_000_000},
        {"ticker": "HI", "price": 50.0, "gap_pct": 19.0, "rel_vol": 9.0,
         "today_vol": 30_000_000, "dollar_vol": 1_500_000_000},
    ):
        sb_new = score_candidate(dict(cand))
        os.environ["SARO_RANK_UPGRADE"] = "0"
        try:
            sb_old = score_candidate(dict(cand))
        finally:
            os.environ["SARO_RANK_UPGRADE"] = "1"
        assert abs(sb_new.total - sb_old.total) < 0.001, cand["ticker"]


def test_gate_off_is_byte_identical_even_with_enrichment_keys():
    c = _base_candidate()
    c.update({"chg_5d_pct": -18.7, "chg_20d_pct": -40.7, "live_gap_pct": 8.11,
              "prior_day_ret_pct": -10.39, "analyst_upside_pct": 95.0,
              "adv20_dollars": 2_240_000_000, "rel_vol_adv20": 1.8})
    os.environ["SARO_RANK_UPGRADE"] = "0"
    try:
        sb_off = score_candidate(c)
        sb_plain = score_candidate(_base_candidate())
    finally:
        os.environ["SARO_RANK_UPGRADE"] = "1"
    # with the gate off, enrichment keys must change NOTHING
    assert sb_off.total == sb_plain.total
    assert sb_off.components["trend"]["raw"] == 0.5
    assert "analyst" not in sb_off.components


# ── stale-quote hard gate (pure helper) ──────────────────────────────────────

def test_stale_gate_rejects_gpc_shape():
    # snapshot said $132.57 (Thursday close); GPC really traded ~$126.15 Monday
    is_stale, diff = _stale_quote_check(132.57, 126.15)
    assert is_stale
    assert diff == pytest.approx(-4.84, abs=0.02)


def test_stale_gate_passes_fresh_quote():
    is_stale, diff = _stale_quote_check(100.0, 102.0)
    assert not is_stale
    assert diff == pytest.approx(2.0, abs=1e-6)


def test_stale_gate_fails_open_on_missing_or_bad_input():
    assert _stale_quote_check(132.57, None) == (False, None)
    assert _stale_quote_check(None, 126.15) == (False, None)
    assert _stale_quote_check(0, 126.15) == (False, None)
    assert _stale_quote_check("junk", "junk") == (False, None)
