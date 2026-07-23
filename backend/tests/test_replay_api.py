"""Unit tests for the Replay API (2026-07-23).

Pure/unit level by design — no live DB, no network:
  * ET session/RTH window math incl. the DST edge (zoneinfo, not UTC dates)
  * compute_replay_levels on synthetic two-day bars with known extremes
  * slice_day_bars overnight vs RTH-only boundaries
  * instrument whitelist + date-range validation
  * holiday 404 path via monkeypatched empty bar query
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.api.routes import replay
from app.api.routes.replay import (
    MIN_DATE,
    compute_replay_levels,
    rth_bounds,
    session_bounds,
    slice_day_bars,
    validate_instrument,
    validate_replay_date,
)

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def bar(ts, o=100.0, h=101.0, l=99.0, c=100.5, v=10):
    return (ts, o, h, l, c, v)


# ── window math ──────────────────────────────────────────────────────────

def test_session_bounds_overnight_starts_prior_calendar_day():
    start, end = session_bounds(date(2026, 7, 21), include_overnight=True)
    assert start == et(2026, 7, 20, 18, 0)
    assert end == et(2026, 7, 21, 16, 0)


def test_session_bounds_rth_only():
    start, end = session_bounds(date(2026, 7, 21), include_overnight=False)
    assert start == et(2026, 7, 21, 9, 30)
    assert end == et(2026, 7, 21, 16, 0)


def test_rth_bounds_dst_edge_utc_offsets():
    """US DST starts Sunday 2026-03-08. Friday before is EST (UTC-5), the
    Monday after is EDT (UTC-4). 09:30 ET must map to 14:30Z then 13:30Z —
    a UTC-date implementation gets one of these wrong."""
    fri_start, _ = rth_bounds(date(2026, 3, 6))
    mon_start, _ = rth_bounds(date(2026, 3, 9))
    assert fri_start.astimezone(timezone.utc).hour == 14
    assert fri_start.astimezone(timezone.utc).minute == 30
    assert mon_start.astimezone(timezone.utc).hour == 13
    assert mon_start.astimezone(timezone.utc).minute == 30


def test_session_bounds_dst_sunday_open():
    """Monday 2026-03-09 overnight opens Sunday 18:00 ET, which is already
    EDT (DST flipped at 2am that Sunday) => 22:00 UTC, not 23:00."""
    start, _ = session_bounds(date(2026, 3, 9), include_overnight=True)
    assert start.astimezone(timezone.utc) == datetime(
        2026, 3, 8, 22, 0, tzinfo=timezone.utc)


# ── slicing ──────────────────────────────────────────────────────────────

def test_slice_day_bars_boundaries():
    day = date(2026, 7, 21)
    bars = [
        bar(et(2026, 7, 20, 17, 59)),   # before session open — excluded
        bar(et(2026, 7, 20, 18, 0)),    # session open — included
        bar(et(2026, 7, 21, 9, 29)),    # overnight tail
        bar(et(2026, 7, 21, 9, 30)),    # RTH open
        bar(et(2026, 7, 21, 15, 59)),   # last RTH bar
        bar(et(2026, 7, 21, 16, 0)),    # 16:00 — excluded (half-open window)
    ]
    overnight = slice_day_bars(bars, day, include_overnight=True)
    assert [b[0] for b in overnight] == [
        et(2026, 7, 20, 18, 0), et(2026, 7, 21, 9, 29),
        et(2026, 7, 21, 9, 30), et(2026, 7, 21, 15, 59)]
    rth = slice_day_bars(bars, day, include_overnight=False)
    assert [b[0] for b in rth] == [
        et(2026, 7, 21, 9, 30), et(2026, 7, 21, 15, 59)]


# ── levels ───────────────────────────────────────────────────────────────

def test_compute_replay_levels_synthetic_two_days():
    day = date(2026, 7, 21)  # Tuesday; prior trading day Monday 7/20

    prior_rth = [
        bar(et(2026, 7, 20, 9, 30), h=100.0, l=95.0, c=98.0),
        bar(et(2026, 7, 20, 12, 0), h=110.0, l=98.0, c=109.0),
        bar(et(2026, 7, 20, 15, 59), h=105.0, l=99.0, c=104.0),
    ]
    session = [
        bar(et(2026, 7, 20, 20, 0), h=101.0, l=97.0),    # Asia only
        bar(et(2026, 7, 21, 1, 30), h=103.0, l=96.0),    # Asia only
        bar(et(2026, 7, 21, 2, 30), h=104.0, l=95.0),    # Asia AND London overlap
        bar(et(2026, 7, 21, 4, 0), h=106.0, l=94.0),     # London only
        bar(et(2026, 7, 21, 5, 30), h=999.0, l=1.0),     # neither window
        bar(et(2026, 7, 21, 9, 30), h=107.0, l=100.0),   # NY open
        bar(et(2026, 7, 21, 15, 59), h=108.0, l=101.0),
    ]

    lv = compute_replay_levels(session, prior_rth, day)
    assert lv["pdh"] == 110.0
    assert lv["pdl"] == 95.0
    assert lv["pdc"] == 104.0
    assert lv["asia_high"] == 104.0   # 02:30 bar counts for Asia (ends 03:00)
    assert lv["asia_low"] == 95.0
    assert lv["london_high"] == 106.0
    assert lv["london_low"] == 94.0
    assert lv["ny_open_ts"] == int(et(2026, 7, 21, 9, 30).timestamp())


def test_compute_replay_levels_empty_prior_day_gives_none():
    day = date(2026, 7, 21)
    lv = compute_replay_levels([], [], day)
    assert lv["pdh"] is None and lv["pdl"] is None and lv["pdc"] is None
    assert lv["asia_high"] is None and lv["london_low"] is None
    assert lv["ny_open_ts"] is None


def test_compute_replay_levels_dst_asia_window():
    """Asia window for Monday 2026-03-09 starts Sunday 18:00 ET = 22:00 UTC
    (EDT). A bar stamped 22:00Z Sunday must be counted; 21:59Z must not."""
    day = date(2026, 3, 9)
    inside = bar(datetime(2026, 3, 8, 22, 0, tzinfo=timezone.utc),
                 h=50.0, l=40.0)
    outside = bar(datetime(2026, 3, 8, 21, 59, tzinfo=timezone.utc),
                  h=999.0, l=1.0)
    lv = compute_replay_levels([outside, inside], [], day)
    assert lv["asia_high"] == 50.0
    assert lv["asia_low"] == 40.0


# ── validation ───────────────────────────────────────────────────────────

def test_validate_instrument_accepts_whitelist_case_insensitive():
    assert validate_instrument("nq") == "NQ"
    assert validate_instrument(" ES ") == "ES"


@pytest.mark.parametrize("inst", ["SPX", "QQQ", "", "GC", "es6", None])
def test_validate_instrument_rejects(inst):
    with pytest.raises(HTTPException) as ei:
        validate_instrument(inst)
    assert ei.value.status_code == 422


def test_validate_replay_date_ok():
    assert validate_replay_date("2026-07-21", date(2026, 7, 23)) == date(2026, 7, 21)


@pytest.mark.parametrize("bad", ["07/21/2026", "2026-13-01", "nope", ""])
def test_validate_replay_date_rejects_bad_format(bad):
    with pytest.raises(HTTPException) as ei:
        validate_replay_date(bad, date(2026, 7, 23))
    assert ei.value.status_code == 422


def test_validate_replay_date_rejects_weekend():
    with pytest.raises(HTTPException) as ei:
        validate_replay_date("2026-07-19", date(2026, 7, 23))  # Sunday
    assert ei.value.status_code == 422


def test_validate_replay_date_rejects_out_of_range():
    for bad in ["2023-04-28", "2030-01-01"]:
        with pytest.raises(HTTPException) as ei:
            validate_replay_date(bad, date(2026, 7, 23))
        assert ei.value.status_code == 422
    # first valid day is fine
    assert validate_replay_date(MIN_DATE.isoformat(),
                                date(2026, 7, 23)) == MIN_DATE


# ── holiday 404 path (monkeypatched empty query) ─────────────────────────

def test_build_day_payload_404_when_no_rth_bars(monkeypatch):
    async def fake_fetch(db, instrument, start, end):
        return []
    monkeypatch.setattr(replay, "_fetch_bars", fake_fetch)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(replay._build_day_payload(
            None, "NQ", date(2026, 7, 3), include_overnight=True))
    assert ei.value.status_code == 404
    assert "holiday" in ei.value.detail.lower()


def test_build_day_payload_success_shape(monkeypatch):
    day = date(2026, 7, 21)
    prior = date(2026, 7, 20)

    def rth_minutes(d):
        start, _ = rth_bounds(d)
        return [bar(start + timedelta(minutes=i)) for i in range(390)]

    async def fake_fetch(db, instrument, start, end):
        # session query starts 18:00 ET 7/20; prior-RTH query starts 09:30 7/20
        if start == session_bounds(day, True)[0]:
            return [bar(et(2026, 7, 20, 20, 0))] + rth_minutes(day)
        if start == rth_bounds(prior)[0]:
            return rth_minutes(prior)
        return []
    monkeypatch.setattr(replay, "_fetch_bars", fake_fetch)

    payload = asyncio.run(replay._build_day_payload(
        None, "NQ", day, include_overnight=False))
    assert payload["date"] == "2026-07-21"
    assert payload["instrument"] == "NQ"
    assert payload["holiday"] is False
    assert len(payload["bars"]) == 390          # RTH-only slice
    first = payload["bars"][0]
    assert set(first) == {"t", "o", "h", "l", "c", "v"}
    assert first["t"] == int(et(2026, 7, 21, 9, 30).timestamp())
    assert payload["levels"]["pdh"] is not None
    assert payload["levels"]["ny_open_ts"] == first["t"]
