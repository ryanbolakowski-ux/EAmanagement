"""LABEL-TRUTH-V1: stop/target REASONS must be generated from the branch that
actually chose the level — never guessed post-hoc — and must follow doctrine:

  * stops default to STRUCTURE: (a) the liquidity-sweep extreme that formed
    the setup, then (b) the nearest valid swing beyond entry, then (c) the
    deterministic tick fallback — VWAP only when explicitly configured;
  * a short's stop is ALWAYS above entry, a long's ALWAYS below (wrong-side
    structure flips to the fallback, never an inverted stop);
  * FVG target references always name their timeframe ("1H FVG ...");
  * every label carries the LEVEL actually used.

Anchor bug: account_signals 42c5c96d (2026-07-05 20:40 ET NQ short 29852/
29902/29702) — numbers right, labels fabricated by post-hoc inference (a
"session VWAP" stop while VWAP sat BELOW entry; an FVG target with no
timeframe).

Pure/offline: synthetic frames only. No network, no DB.

Run: pytest backend/tests/test_signal_label_truth.py -v -p no:cacheprovider
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig


# ── Helpers ──────────────────────────────────────────────────────────────

def _cfg(**over):
    base = dict(
        name="Label Truth Test", instruments=["NQ"],
        primary_timeframe="15m", execution_timeframe="1m",
        higher_timeframes=["1H"], risk_reward_ratio=2.5,
        stop_loss_type="structure",
    )
    base.update(over)
    c = StrategyConfig(**base)
    c.rule_tree = {}
    return c


def _strat(**over) -> ICTStrategy:
    return ICTStrategy(_cfg(**over), instrument="NQ")


def _frame(rows, start="2026-07-01 14:00", freq="1min"):
    """rows: list of (open, high, low, close). UTC DatetimeIndex."""
    idx = pd.date_range(start, periods=len(rows), freq=freq, tz="UTC")
    return pd.DataFrame(
        [{"open": o, "high": h, "low": l, "close": c, "volume": 1000}
         for (o, h, l, c) in rows],
        index=idx,
    )


def _flat_with_pivot_high(n=25, base=29850.0, pivot_at=12, pivot_high=29900.0):
    rows = []
    for i in range(n):
        hi = pivot_high if i == pivot_at else base + 10.0
        rows.append((base, hi, base - 10.0, base + 5.0))
    return _frame(rows)


def _flat_with_pivot_low(n=25, base=29850.0, pivot_at=12, pivot_low=29810.0):
    rows = []
    for i in range(n):
        lo = pivot_low if i == pivot_at else base - 10.0
        rows.append((base, base + 10.0, lo, base - 5.0))
    return _frame(rows)


ASIA_TS = datetime(2026, 7, 6, 0, 40, tzinfo=timezone.utc)  # 20:40 ET Jul 5


# ── Stop: doctrine hierarchy + labels ────────────────────────────────────

def test_stop_sweep_branch_wins_over_swing_and_labels_the_level():
    """(a) sweep extreme beats (b) swing: with BOTH available the stop anchors
    to the sweep, and the label names the session sweep + the level used."""
    s = _strat()
    df = _flat_with_pivot_high()  # swing high 29,900 also present
    sl = s._compute_stop_loss(29852.0, "short", df, exec_df=None,
                              sweep_level=29901.5)
    assert sl == pytest.approx(29902.0)  # sweep 29,901.5 + 2-tick buffer
    assert s._last_stop_choice["branch"] == "sweep"
    reason = s._compose_stop_reason("short", ASIA_TS, "1m", "1m")
    assert "sweep high" in reason, reason
    assert "Asia session" in reason, reason      # 20:40 ET = Asia session
    assert "29,902" in reason, reason            # the LEVEL actually used


def test_stop_swing_branch_when_no_sweep_and_labels_tf_and_level():
    """(b) nearest valid swing beyond entry when no sweep formed the setup."""
    s = _strat()
    df = _flat_with_pivot_high()
    sl = s._compute_stop_loss(29852.0, "short", df, exec_df=df,
                              sweep_level=None)
    assert sl == pytest.approx(29900.5)  # swing 29,900 + 2-tick buffer
    assert s._last_stop_choice["branch"] == "swing"
    reason = s._compose_stop_reason("short", ASIA_TS, "1m", "15m")
    assert reason == "1m swing high 29,900.5", reason


def test_stop_long_swing_below_entry():
    s = _strat()
    df = _flat_with_pivot_low()
    sl = s._compute_stop_loss(29852.0, "long", df, exec_df=df, sweep_level=None)
    assert sl == pytest.approx(29809.5)  # swing low 29,810 - 2-tick buffer
    assert sl < 29852.0
    assert s._last_stop_choice["branch"] == "swing"
    reason = s._compose_stop_reason("long", ASIA_TS, "1m", "15m")
    assert reason == "1m swing low 29,809.5", reason


def test_stop_capped_swing_is_not_labelled_as_swing():
    """When the max-stop cap binds, the stop is NOT at the swing — the label
    must say cap, never claim a structure level the stop doesn't sit at."""
    s = _strat()
    df = _flat_with_pivot_low(pivot_low=29780.0)  # raw stop 29,779.5 < cap 29,802
    sl = s._compute_stop_loss(29852.0, "long", df, exec_df=df, sweep_level=None)
    assert sl == pytest.approx(29802.0)  # entry - 200 ticks (50 pts)
    assert s._last_stop_choice["capped"] is True
    reason = s._compose_stop_reason("long", ASIA_TS, "1m", "15m")
    assert "max-risk cap" in reason, reason
    assert "29,802" in reason, reason
    assert "swing low 29,7" not in reason, reason


def test_stop_wrong_side_structure_falls_back_short():
    """(c) structure entirely on the wrong side of entry -> tick fallback on
    the CORRECT side, labelled as the fallback (inverted-stop guard)."""
    s = _strat()
    df = _flat_with_pivot_high()          # all highs < 29,999
    entry = 29999.0
    sl = s._compute_stop_loss(entry, "short", df, exec_df=df, sweep_level=None)
    assert s._last_stop_choice["branch"] == "ticks_fallback"
    assert sl == pytest.approx(entry + 12 * 0.25)
    assert sl > entry                      # short stop ABOVE entry, always
    reason = s._compose_stop_reason("short", ASIA_TS, "1m", "15m")
    assert "12-tick fallback" in reason, reason
    assert "30,002" in reason, reason


def test_enforce_stop_side_guard_flips_inverted_stops():
    """Defense-in-depth: even if a branch ever produced an inverted stop, the
    on_bar guard flips it to the fallback on the correct side."""
    s = _strat()
    flipped, reason = s._enforce_stop_side(100.0, 101.0, "long")
    assert flipped == pytest.approx(100.0 - 12 * 0.25)
    assert flipped < 100.0
    assert reason and "wrong side" in reason
    flipped, reason = s._enforce_stop_side(100.0, 99.0, "short")
    assert flipped == pytest.approx(100.0 + 12 * 0.25)
    assert flipped > 100.0
    assert reason and "wrong side" in reason
    # No-op on healthy stops — backtest numbers untouched.
    same, none_reason = s._enforce_stop_side(100.0, 98.0, "long")
    assert same == 98.0 and none_reason is None
    same, none_reason = s._enforce_stop_side(100.0, 102.0, "short")
    assert same == 102.0 and none_reason is None


def test_session_label_for_asia_and_dead_zone():
    s = _strat()
    assert s._session_label_for(ASIA_TS) == "Asia session"
    dead = datetime(2026, 7, 6, 16, 30, tzinfo=timezone.utc)  # 12:30 ET
    assert s._session_label_for(dead) is None


# ── Target: branch truth + FVG timeframe naming ──────────────────────────

def test_target_swing_branch_label():
    s = _strat()
    # Short from 29,852; clear swing low at 29,700 (>= 1R away). Risk 53 pts
    # so the 3R clamp (159) does not bind on the 151.5-pt swing target.
    df = _flat_with_pivot_low(n=30, base=29840.0, pivot_at=15, pivot_low=29700.0)
    tp = s._compute_take_profit(29852.0, 29905.0, "short", df, htf_df=None)
    assert tp == pytest.approx(29700.5)  # swing + 2-tick buffer
    assert s._last_tp_choice["branch"] == "swing"
    reason = s._compose_target_reason("short", "15m")
    assert reason == "15m prior swing low 29,700.5", reason


def test_anchor_signal_geometry_would_be_labelled_3r_cap():
    """The real 42c5c96d geometry (entry 29,852 / stop 29,902 / target 29,702):
    a 29,700.5 swing target sits 151.5 pts out but risk is 50 -> the 3R clamp
    binds at 29,702. The truthful label is the CAP — not 'FVG invalidation'."""
    s = _strat()
    df = _flat_with_pivot_low(n=30, base=29840.0, pivot_at=15, pivot_low=29700.0)
    tp = s._compute_take_profit(29852.0, 29902.0, "short", df, htf_df=None)
    assert tp == pytest.approx(29702.0)
    assert s._last_tp_choice["branch"] == "rr_cap"
    reason = s._compose_target_reason("short", "15m")
    assert reason.startswith("3R cap"), reason
    assert "29,702" in reason, reason


def test_target_htf_fvg_branch_names_timeframe():
    """Doctrine: an FVG target must name its timeframe — '1H FVG', never a
    bare 'FVG'."""
    s = _strat()
    s._last_htf_tf = "1H"  # what _get_htf_data records when it picks the 1H frame
    # Bearish 1H FVG: c3.high < c1.low -> gap 29,750..29,800, CE 29,775.
    rows = [(29900, 29910, 29890, 29895)] * 5
    rows += [(29890, 29895, 29800, 29805)]   # c1: low 29,800
    rows += [(29800, 29805, 29760, 29765)]   # c2: displacement
    rows += [(29745, 29750, 29700, 29710)]   # c3: high 29,750 < c1.low
    rows += [(29710, 29740, 29695, 29720)] * 4  # stays below the gap (unfilled)
    htf = _frame(rows, freq="1h")
    # risk 30 pts so CE (77 pts away) >= 1.5R and 3R (90) doesn't clamp.
    tp = s._compute_take_profit(29852.0, 29882.0, "short", None, htf_df=htf)
    assert tp == pytest.approx(29775.0)
    assert s._last_tp_choice["branch"] == "htf_fvg"
    reason = s._compose_target_reason("short", "15m")
    assert reason == "1H FVG (midpoint) 29,775", reason


def test_target_rr_fallback_label():
    s = _strat()
    tp = s._compute_take_profit(29852.0, 29882.0, "short", None, htf_df=None)
    assert tp == pytest.approx(29852.0 - 30.0 * 2.5)
    assert s._last_tp_choice["branch"] == "rr"
    reason = s._compose_target_reason("short", "15m")
    assert "2.5R target" in reason, reason
    assert "no clean structure" in reason, reason


def test_target_3r_clamp_is_labelled_as_cap_not_structure():
    """When the 3R clamp moves the target off the structure level, the label
    must say so — never claim the FVG/swing the target no longer sits at."""
    s = _strat()
    s._last_htf_tf = "4H"
    rows = [(29900, 29910, 29890, 29895)] * 5
    rows += [(29890, 29895, 29800, 29805)]
    rows += [(29800, 29805, 29760, 29765)]
    rows += [(29745, 29750, 29700, 29710)]
    rows += [(29710, 29740, 29695, 29720)] * 4
    htf = _frame(rows, freq="4h")
    # risk 20 pts: CE 77 pts away passes the 1.5R gate but 3R = 60 clamps it.
    tp = s._compute_take_profit(29852.0, 29872.0, "short", None, htf_df=htf)
    assert tp == pytest.approx(29852.0 - 60.0)
    assert s._last_tp_choice["branch"] == "rr_cap"
    reason = s._compose_target_reason("short", "15m")
    assert reason.startswith("3R cap"), reason
    assert "29,792" in reason, reason
    assert "4H FVG" in reason  # names what it capped, not what it hit


def test_target_range_mode_label():
    s = _strat()
    s.config.take_profit_mode = "range"
    df = _flat_with_pivot_low(n=30, base=29850.0, pivot_at=15, pivot_low=29700.0)
    # risk 60 pts so the 3R clamp (180) does not bind on the 151.5-pt target.
    tp = s._compute_take_profit(29852.0, 29912.0, "short", df, htf_df=None)
    assert tp == pytest.approx(29700.5)
    assert s._last_tp_choice["branch"] == "range"
    reason = s._compose_target_reason("short", "15m")
    assert "range low" in reason, reason
    assert "29,700.5" in reason, reason


def test_get_htf_data_records_tf_label():
    s = _strat()
    df = _flat_with_pivot_high()
    assert s._get_htf_data({"1H": df}, ["1H"]) is df
    assert s._last_htf_tf == "1H"
    assert s._get_htf_data({}, []) is None
    assert s._last_htf_tf is None


# ── Fallback inference (level_reasons): doctrine hardening ───────────────

def _flat_inference_frame(n=60, high=29870.0, low=29845.0, close=29860.0,
                          start="2026-07-06 16:30"):
    """Flat single-ET-day frame in the 12:30 ET dead zone (no named session
    levels compete). VWAP ~ (high+low+close)/3, below any short entry above it."""
    rows = [(close, high, low, close)] * n
    return _frame(rows, start=start, freq="1min")


def test_inference_never_labels_short_stop_with_below_entry_level():
    """THE 20:40 BUG CLASS: a short's stop (above entry) must never be
    attributed to a level sitting BELOW entry (the fabricated 'session VWAP'
    stop). With every detected level below entry, the stop label falls back —
    it does not borrow an impossible level."""
    from app.engines.level_reasons import infer_stop_target_reasons
    df = _flat_inference_frame()  # VWAP ~29,858, swings <= 29,870 — all < entry
    out = infer_stop_target_reasons(
        direction="short", entry=29880.0, stop=29902.0, target=29700.0,
        bars_df=df, instrument="NQ",
    )
    assert out["stop_reason"] == "strategy stop", out
    assert "VWAP" not in out["stop_reason"]


def test_inference_vwap_stop_only_when_explicitly_allowed():
    """Doctrine: VWAP is a valid stop basis ONLY when the strategy config
    explicitly uses a VWAP stop (allow_vwap_stop=True)."""
    from app.engines.level_reasons import infer_stop_target_reasons
    # VWAP == 29,902, exactly at the stop and ABOVE entry (side-sane).
    df = _frame([(29902.0, 29912.0, 29892.0, 29902.0)] * 60,
                start="2026-07-06 16:30", freq="1min")
    kw = dict(direction="short", entry=29852.0, stop=29902.0, target=29700.0,
              bars_df=df, instrument="NQ")
    out = infer_stop_target_reasons(**kw)
    assert out["stop_reason"] != "session VWAP", out       # gated off by default
    out = infer_stop_target_reasons(**kw, allow_vwap_stop=True)
    assert out["stop_reason"] == "session VWAP", out       # explicit opt-in only


def test_inference_labels_carry_bars_timeframe():
    """FVG/swing labels from the inference fallback name the bars' timeframe
    when the caller provides it (doctrine: FVGs must carry a timeframe).
    Dead-zone timestamps (12:30 ET) so no named session level competes."""
    from app.engines.level_reasons import infer_stop_target_reasons
    rows = []
    for i in range(60):
        lo = 29800.0 if i == 30 else 29840.0
        rows.append((29850.0, 29860.0, lo, 29845.0))
    df = _frame(rows, start="2026-07-06 16:30", freq="1min")
    out = infer_stop_target_reasons(
        direction="long", entry=29852.0, stop=29800.0, target=30100.0,
        bars_df=df, instrument="NQ", bars_tf_label="5m",
    )
    assert out["stop_reason"] == "5m swing low", out
