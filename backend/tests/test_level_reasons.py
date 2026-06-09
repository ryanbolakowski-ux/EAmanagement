"""Unit tests for app.engines.level_reasons.infer_stop_target_reasons.

Pure/offline: builds synthetic OHLC frames and asserts the inferred
stop_reason / target_reason. No network, no DB, no app bootstrap.
"""
import pandas as pd
from datetime import datetime, timezone, timedelta

from app.engines.level_reasons import infer_stop_target_reasons


def _bar(ts, o, h, low, c, v=1000):
    return {"timestamp": ts, "open": o, "high": h, "low": low, "close": c, "volume": v}


def _intraday_frame(start_utc, n=60, step_min=5, base=100.0, drift=0.05,
                    swing_low_at=None, swing_low_px=None,
                    swing_high_at=None, swing_high_px=None):
    """Build an ascending intraday frame; optionally carve a single isolated
    swing low / swing high at a given bar index so it is a clear pivot."""
    rows = []
    price = base
    for i in range(n):
        ts = start_utc + timedelta(minutes=step_min * i)
        lo = price - 0.30
        hi = price + 0.30
        if swing_low_at is not None and i == swing_low_at:
            lo = swing_low_px
        if swing_high_at is not None and i == swing_high_at:
            hi = swing_high_px
        rows.append(_bar(ts, price, hi, lo, price + 0.10))
        price += drift
    return pd.DataFrame(rows)


def test_long_stop_at_swing_low():
    """A long whose stop sits at a recent swing low → stop_reason 'swing low'.

    Timestamps are placed in the 12:30 ET 'dead zone' (outside Asian/London/
    NY-AM windows) and span a single ET date, so no *named* session/previous
    level competes — the swing low is the unambiguous match for the stop."""
    start = datetime(2026, 6, 9, 16, 30, tzinfo=timezone.utc)  # ~12:30 ET
    df = _intraday_frame(start, n=60, swing_low_at=30, swing_low_px=95.00)
    out = infer_stop_target_reasons(
        direction="long", entry=101.0, stop=95.00, target=130.0,
        bars_df=df, instrument="ES",
    )
    assert out["stop_reason"] == "swing low", out


def test_long_target_at_previous_day_high():
    """A long whose target ~ previous-day high → target_reason mentions
    'previous high'."""
    # Day 1 (prior ET date): a session whose max high is 120.00.
    d1 = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)  # ~10:00 ET Jun 8
    day1 = []
    p = 110.0
    for i in range(30):
        ts = d1 + timedelta(minutes=5 * i)
        hi = 120.00 if i == 15 else p + 0.25
        day1.append(_bar(ts, p, hi, p - 0.25, p + 0.1))
        p += 0.02
    # Day 2 (latest ET date): price trades up toward the prior high.
    d2 = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)  # ~10:00 ET Jun 9
    day2 = []
    p = 112.0
    for i in range(30):
        ts = d2 + timedelta(minutes=5 * i)
        day2.append(_bar(ts, p, p + 0.25, p - 0.25, p + 0.1))
        p += 0.05
    df = pd.DataFrame(day1 + day2)
    out = infer_stop_target_reasons(
        direction="long", entry=113.0, stop=108.0, target=120.00,
        bars_df=df, instrument="ES",
    )
    assert "previous high" in out["target_reason"], out


def test_no_match_falls_back():
    """Stop/target far from every detected level → generic fallbacks."""
    start = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    df = _intraday_frame(start, n=60)
    out = infer_stop_target_reasons(
        direction="long", entry=101.0, stop=10.0, target=9999.0,
        bars_df=df, instrument="ES",
    )
    assert out["stop_reason"] == "strategy stop", out
    assert out["target_reason"] == "strategy target", out


def test_short_mirror_swing_high_and_session_low():
    """A short: stop at a swing high, target at a session low (mirror case).

    Dead-zone timestamps (12:30 ET) so no named session level competes; the
    isolated swing high is the stop match and the frame's minimum low (= the
    last bar's low, captured by the swing detector) is the target match."""
    start = datetime(2026, 6, 9, 16, 30, tzinfo=timezone.utc)  # ~12:30 ET
    # Descending frame with an isolated swing high at bar 30, and the session
    # low is the frame's minimum low.
    rows = []
    price = 120.0
    for i in range(60):
        ts = start + timedelta(minutes=5 * i)
        hi = 130.00 if i == 30 else price + 0.30
        lo = price - 0.30
        rows.append(_bar(ts, price, hi, lo, price - 0.10))
        price -= 0.05
    df = pd.DataFrame(rows)
    # Last bar low is the session low; grab it for the target.
    session_low = float(df["low"].min())
    out = infer_stop_target_reasons(
        direction="short", entry=118.0, stop=130.00, target=session_low,
        bars_df=df, instrument="ES",
    )
    assert out["stop_reason"] == "swing high", out
    # Target should resolve to one of the low-side levels (NY AM low / swing
    # low / session low) — never the generic fallback.
    assert out["target_reason"] != "strategy target", out
    assert "low" in out["target_reason"], out


def test_empty_frame_falls_back_no_crash():
    """An empty bars_df must return the fallbacks and never raise."""
    out = infer_stop_target_reasons(
        direction="long", entry=100.0, stop=99.0, target=110.0,
        bars_df=pd.DataFrame(), instrument="ES",
    )
    assert out == {"stop_reason": "strategy stop", "target_reason": "strategy target"}


def test_none_frame_falls_back_no_crash():
    """A None bars_df must return the fallbacks and never raise."""
    out = infer_stop_target_reasons(
        direction="long", entry=100.0, stop=99.0, target=110.0,
        bars_df=None, instrument=None,
    )
    assert out == {"stop_reason": "strategy stop", "target_reason": "strategy target"}


def test_reasons_never_blank():
    """Whatever the inputs, neither label is ever an empty string."""
    start = datetime(2026, 6, 9, 14, 0, tzinfo=timezone.utc)
    df = _intraday_frame(start, n=20)
    for d in ("long", "short", "", "weird"):
        out = infer_stop_target_reasons(
            direction=d, entry=101.0, stop=100.0, target=105.0,
            bars_df=df, instrument="ES",
        )
        assert out["stop_reason"], (d, out)
        assert out["target_reason"], (d, out)
