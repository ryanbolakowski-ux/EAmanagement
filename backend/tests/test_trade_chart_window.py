"""Pure window + label tests for the signal trade-chart (CHART-TRUTH-V1).

The 2026-07-13 10:45 ET NQ short email chart plotted delayed proxy bars whose
x axis (raw UTC, no fire marker) appeared to end BEFORE the signal fired.
These tests pin the extracted pure helpers: the [fire-45m, fire+45m]-clipped-
to-now bar window and the Eastern 'HH:MM ET' tick labels. No rendering here.
"""
from datetime import datetime, timedelta, timezone

from app.services.trade_chart import compute_signal_window, format_et_label

UTC = timezone.utc


# ── compute_signal_window ────────────────────────────────────────────────

def test_window_full_when_history_available():
    fire = datetime(2026, 7, 13, 14, 45, tzinfo=UTC)
    start, end = compute_signal_window(fire, now=fire + timedelta(hours=2))
    assert start == datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 13, 15, 30, tzinfo=UTC)


def test_window_clips_to_now_at_send_time():
    # At send time now == fire: no post-entry bars exist yet, so the window
    # must end AT the fire time — never pretend future candles exist.
    fire = datetime(2026, 7, 13, 14, 45, tzinfo=UTC)
    start, end = compute_signal_window(fire, now=fire)
    assert start == fire - timedelta(minutes=45)
    assert end == fire


def test_window_clips_to_now_partial_followthrough():
    fire = datetime(2026, 7, 13, 14, 45, tzinfo=UTC)
    now = fire + timedelta(minutes=10)
    _start, end = compute_signal_window(fire, now=now)
    assert end == now


def test_window_never_ends_before_fire_time():
    # Skewed clock (now < fire) must not reproduce the bug where the chart
    # stops before the entry.
    fire = datetime(2026, 7, 13, 14, 45, tzinfo=UTC)
    _start, end = compute_signal_window(fire, now=fire - timedelta(minutes=30))
    assert end == fire


def test_window_is_tz_aware_utc_even_for_naive_input():
    fire = datetime(2026, 7, 13, 14, 45)  # naive == UTC by convention
    start, end = compute_signal_window(fire, now=datetime(2026, 7, 13, 16, 0))
    assert start.tzinfo is not None and start.utcoffset() == timedelta(0)
    assert end.tzinfo is not None and end.utcoffset() == timedelta(0)
    assert start == datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 13, 15, 30, tzinfo=UTC)


def test_window_normalizes_non_utc_aware_input():
    edt = timezone(timedelta(hours=-4))  # fixed EDT offset
    fire = datetime(2026, 7, 13, 10, 45, tzinfo=edt)  # == 14:45 UTC
    start, end = compute_signal_window(
        fire, now=datetime(2026, 7, 13, 17, 0, tzinfo=UTC))
    assert start == datetime(2026, 7, 13, 14, 0, tzinfo=UTC)
    assert end == datetime(2026, 7, 13, 15, 30, tzinfo=UTC)


def test_window_honors_custom_before_after():
    fire = datetime(2026, 7, 13, 14, 45, tzinfo=UTC)
    start, end = compute_signal_window(fire, now=fire + timedelta(hours=3),
                                       before_min=10, after_min=20)
    assert start == fire - timedelta(minutes=10)
    assert end == fire + timedelta(minutes=20)


# ── format_et_label ──────────────────────────────────────────────────────

def test_label_is_eastern_wall_clock_edt():
    # 2026-07-13 is an EDT date (UTC-4): 14:45 UTC == 10:45 ET — exactly the
    # fire time of the mis-read NQ short.
    assert format_et_label(datetime(2026, 7, 13, 14, 45, tzinfo=UTC)) == "10:45 ET"


def test_label_is_eastern_wall_clock_est():
    # 2026-01-13 is an EST date (UTC-5): 14:45 UTC == 09:45 ET.
    assert format_et_label(datetime(2026, 1, 13, 14, 45, tzinfo=UTC)) == "09:45 ET"


def test_label_naive_input_treated_as_utc():
    assert format_et_label(datetime(2026, 7, 13, 14, 45)) == "10:45 ET"


def test_label_always_carries_et_suffix():
    assert format_et_label(datetime(2026, 7, 13, 0, 3, tzinfo=UTC)).endswith(" ET")
