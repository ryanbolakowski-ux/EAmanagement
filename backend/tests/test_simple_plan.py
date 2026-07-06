"""'Analyze any ticker' plain-English game plan (owner request 2026-07-06):
wait-for price -> buy -> hold-to target -> long-term analyst target.
_compose_simple_plan is pure — these run with zero I/O."""
from app.engines.options.theta_scanner import _compose_simple_plan

LV = {"entry": 100.0, "stop": 95.0, "target": 112.0, "rr": 2.4, "ok": True,
      "projected_move_pct": 12.0}
ANALYST = {"target": 130.0, "rating": "Buy", "analysts": 12}


def _plan(**kw):
    base = dict(ticker="TEST", price=100.0, would_be_pick=False,
                is_candidate=False, above_vwap=True, overext=False,
                illiquid=False, vwap=98.0, swing_low=94.0, lv=dict(LV),
                analyst=dict(ANALYST))
    base.update(kw)
    return _compose_simple_plan(**base)


def test_pick_is_buy_now():
    p = _plan(would_be_pick=True)
    assert p["action"] == "buy_now"
    assert p["buy_at"] == 100.0 and p["buy_when"] == "today"
    assert p["sell_at"] == 112.0 and p["stop_at"] == 95.0
    assert "buy" in p["steps"][0].lower()


def test_overextended_waits_for_vwap_dip():
    p = _plan(overext=True, vwap=92.0)
    assert p["action"] == "wait" and p["buy_how"] == "dip_to"
    assert p["buy_at"] == 92.0
    assert p["buy_when"] == "in the next few days"  # 8% away
    assert "dip" in p["steps"][0].lower()


def test_below_vwap_waits_for_reclaim():
    p = _plan(is_candidate=True, above_vwap=False, vwap=101.5)
    assert p["buy_how"] == "break_above" and p["buy_at"] == 101.5
    assert "climb back above" in p["steps"][0]


def test_no_setup_uses_best_support():
    p = _plan()  # vwap 98 beats swing low 94
    assert p["buy_how"] == "dip_to" and p["buy_at"] == 98.0


def test_no_support_stands_aside():
    p = _plan(vwap=None, swing_low=None)
    assert p["action"] == "stand_aside"
    assert p["sell_at"] is None and p["stop_at"] is None


def test_illiquid_skips():
    p = _plan(illiquid=True)
    assert p["action"] == "skip"
    assert "skip" in p["headline"].lower()


def test_stop_above_dip_entry_is_repaired():
    # dip-buy at 90 with a structure stop at 95 would be nonsense
    p = _plan(vwap=90.0, swing_low=None, lv={**LV, "stop": 95.0})
    assert p["buy_at"] == 90.0
    assert p["stop_at"] == round(90.0 * 0.97, 2)
    assert "under your entry" in p["stop_note"]


def test_analyst_below_price_warns():
    p = _plan(analyst={"target": 80.0, "rating": "Sell", "analysts": 5})
    assert p["long_term"]["upside_pct"] == -20.0
    assert any("BELOW" in s for s in p["steps"])


def test_no_analyst_coverage_degrades():
    p = _plan(analyst=None)
    assert p["long_term"] is None
    assert "no wall street analyst coverage" in " ".join(p["steps"]).lower()


def test_upside_reaches_headline():
    p = _plan(would_be_pick=True)  # analyst +30%
    assert "hold long-term toward $130.00" in p["headline"]


def test_faraway_support_stands_aside():
    # a swing low 60% below price is NOT a dip-buy level (ZCMD regression)
    p = _plan(vwap=None, swing_low=40.0)
    assert p["action"] == "stand_aside"


def test_money_formatting_two_decimals():
    p = _plan(would_be_pick=True, lv={**LV, "target": 112.5})
    assert "$112.50" in p["steps"][2]
