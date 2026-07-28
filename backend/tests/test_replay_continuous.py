"""Unit tests for the CONTINUOUS multi-day Replay API (2026-07-28).

Pure/unit by design — no live DB, no network, no email:
  * weekday-trading-day walking (context/forward window edges)
  * RTH-bar predicate + kind slicing (RTH vs ETH)
  * day_starts assembly (RTH opens, overnight bars never open a day)
  * continuous window assembly + playhead at the RTH open, incl. a DST edge
  * /continuous/more pagination continuity (strictly after after_ts, no gap)
  * blind /continuous/random caps the window at yesterday ET

DB access is exercised by monkeypatching replay._fetch_bars / _count_rth_bars
/ _data_range, exactly like test_replay_api.py.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.api.routes import replay
from app.api.routes.replay import (
    _assemble_continuous,
    _assemble_more,
    _build_day_starts,
    _is_rth_bar,
    _last_complete_trading_day,
    _next_trading_day,
    _prev_trading_day,
    _slice_kind,
    _walk_trading_days,
)

ET = ZoneInfo("America/New_York")


def et(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def bar(ts, o=100.0, h=101.0, l=99.0, c=100.5, v=10):
    return (ts, o, h, l, c, v)


def rth_bars(d: date):
    """390 one-minute RTH bars 09:30..15:59 ET for date `d`."""
    start = et(d.year, d.month, d.day, 9, 30)
    return [bar(start + timedelta(minutes=i)) for i in range(390)]


def overnight_bars(d: date):
    """Sparse overnight context for trading day `d`: 18:00 (d-1) .. 09:29 d,
    one bar every 30 min. Enough to prove ETH bars sit *before* the RTH open
    without ballooning the fixture."""
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:
        prev = prev - timedelta(days=1)
    out = []
    t = et(prev.year, prev.month, prev.day, 18, 0)
    open_ = et(d.year, d.month, d.day, 9, 30)
    while t < open_:
        out.append(bar(t))
        t = t + timedelta(minutes=30)
    return out


def make_fetch(dates, include_overnight=False, holidays=frozenset()):
    """Build a fake _fetch_bars over a fixed set of trading `dates`. Returns
    only the bars whose timestamp lands in the requested [start, end) window,
    so window clamping is genuinely exercised."""
    store = []
    for d in dates:
        if d in holidays:
            continue
        if include_overnight:
            store.extend(overnight_bars(d))
        store.extend(rth_bars(d))
    store.sort(key=lambda b: b[0])

    async def _fetch(db, instrument, start, end):
        return [b for b in store if start <= b[0] < end]

    return _fetch


# ── weekday-day walking ──────────────────────────────────────────────────

def test_walk_trading_days_skips_weekends():
    fri = date(2026, 7, 24)   # Friday
    mon = date(2026, 7, 27)   # following Monday
    assert _next_trading_day(fri) == mon
    assert _prev_trading_day(mon) == fri
    assert _walk_trading_days(fri, 1) == mon
    assert _walk_trading_days(mon, -1) == fri
    assert _walk_trading_days(fri, 0) == fri


def test_walk_trading_days_is_reversible():
    start = date(2026, 3, 18)  # Wednesday
    for k in range(0, 25):
        assert _walk_trading_days(_walk_trading_days(start, -k), k) == start


# ── RTH predicate + slicing ──────────────────────────────────────────────

@pytest.mark.parametrize("hh,mm,expected", [
    (9, 29, False), (9, 30, True), (12, 0, True),
    (15, 59, True), (16, 0, False), (20, 0, False), (2, 0, False),
])
def test_is_rth_bar_boundaries(hh, mm, expected):
    assert _is_rth_bar(et(2026, 7, 21, hh, mm)) is expected


def test_slice_kind_rth_drops_overnight():
    day = date(2026, 7, 21)
    bars = overnight_bars(day) + rth_bars(day)
    assert _slice_kind(bars, include_overnight=True) == list(bars)
    rth = _slice_kind(bars, include_overnight=False)
    assert len(rth) == 390
    assert all(_is_rth_bar(b[0]) for b in rth)


# ── day_starts ───────────────────────────────────────────────────────────

def test_build_day_starts_multiday_rth():
    days = [date(2026, 7, 21), date(2026, 7, 22), date(2026, 7, 23)]
    bars = []
    for d in days:
        bars.extend(rth_bars(d))
    ds = _build_day_starts(bars)
    assert [e["date"] for e in ds] == [d.isoformat() for d in days]
    assert [e["index"] for e in ds] == [0, 390, 780]
    # each index points at that day's 09:30 open
    for e in ds:
        assert bars[e["index"]][0].astimezone(ET).strftime("%H:%M") == "09:30"


def test_build_day_starts_overnight_never_opens_a_day():
    day = date(2026, 7, 21)
    bars = overnight_bars(day) + rth_bars(day)
    ds = _build_day_starts(bars)
    assert len(ds) == 1
    assert ds[0]["date"] == day.isoformat()
    # the open bar is the 09:30 RTH bar, i.e. right after the overnight block
    assert bars[ds[0]["index"]][0].astimezone(ET).strftime("%H:%M") == "09:30"
    assert ds[0]["index"] == len(overnight_bars(day))


# ── cap helper ───────────────────────────────────────────────────────────

def test_last_complete_trading_day_is_yesterday_when_data_is_today():
    now = datetime(2026, 7, 28, 11, 0, tzinfo=ET)  # Tuesday mid-session
    assert _last_complete_trading_day(date(2026, 7, 28), now) == date(2026, 7, 27)


def test_last_complete_trading_day_tracks_data_when_data_is_older():
    now = datetime(2026, 7, 28, 11, 0, tzinfo=ET)
    assert _last_complete_trading_day(date(2026, 7, 20), now) == date(2026, 7, 20)


# ── continuous window assembly + playhead ────────────────────────────────

def test_assemble_continuous_window_and_playhead(monkeypatch):
    # Mon..Fri of one week; start Wed with 1 day context + 1 day forward.
    week = [date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22),
            date(2026, 7, 23), date(2026, 7, 24)]
    monkeypatch.setattr(replay, "_fetch_bars", make_fetch(week))

    async def fake_count(db, inst, day):
        return 390 if day in week else 0
    monkeypatch.setattr(replay, "_count_rth_bars", fake_count)

    payload = asyncio.run(_assemble_continuous(
        None, "NQ", start_date=date(2026, 7, 22), context_days=1,
        forward_days=1, include_overnight=False,
        first_data_day=date(2026, 7, 20), cap_date=date(2026, 7, 24)))

    # context 1 back = Tue 7/21, forward 1 = Thu 7/23
    assert [d["date"] for d in payload["day_starts"]] == [
        "2026-07-21", "2026-07-22", "2026-07-23"]
    assert payload["first_date"] == "2026-07-21"
    assert payload["last_date"] == "2026-07-23"
    assert payload["start_date"] == "2026-07-22"
    # playhead sits at Wed 09:30 open (index 390: one full RTH day of context)
    assert payload["playhead_index"] == 390
    ph = payload["bars"][payload["playhead_index"]]
    assert ph["t"] == int(et(2026, 7, 22, 9, 30).timestamp())
    # Fri 7/24 exists within cap -> more forward available
    assert payload["has_more_forward"] is True


def test_assemble_continuous_playhead_at_open_across_dst(monkeypatch):
    # US DST starts Sun 2026-03-08. Fri 3/6 is EST (UTC-5); Mon 3/9 is EDT
    # (UTC-4). Start Monday with Friday as context.
    days = [date(2026, 3, 6), date(2026, 3, 9)]
    monkeypatch.setattr(replay, "_fetch_bars", make_fetch(days))

    async def fake_count(db, inst, day):
        return 0  # no forward days -> has_more_forward False (isolates playhead)
    monkeypatch.setattr(replay, "_count_rth_bars", fake_count)

    payload = asyncio.run(_assemble_continuous(
        None, "NQ", start_date=date(2026, 3, 9), context_days=1,
        forward_days=1, include_overnight=False,
        first_data_day=date(2026, 3, 6), cap_date=date(2026, 3, 13)))

    ds = {e["date"]: e["index"] for e in payload["day_starts"]}
    # Friday 09:30 EST -> 14:30 UTC
    fri_open = payload["bars"][ds["2026-03-06"]]
    assert datetime.fromtimestamp(fri_open["t"], timezone.utc).hour == 14
    assert datetime.fromtimestamp(fri_open["t"], timezone.utc).minute == 30
    # Monday (start) 09:30 EDT -> 13:30 UTC, and this is the playhead
    assert payload["playhead_index"] == ds["2026-03-09"]
    mon_open = payload["bars"][payload["playhead_index"]]
    assert datetime.fromtimestamp(mon_open["t"], timezone.utc).hour == 13
    assert datetime.fromtimestamp(mon_open["t"], timezone.utc).minute == 30
    assert mon_open["t"] == int(et(2026, 3, 9, 9, 30).timestamp())


def test_assemble_continuous_404_when_start_is_holiday(monkeypatch):
    # start_date 7/22 is a holiday (no bars); neighbours exist.
    week = [date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 23),
            date(2026, 7, 24)]
    monkeypatch.setattr(replay, "_fetch_bars",
                        make_fetch(week, holidays=frozenset()))

    async def fake_count(db, inst, day):
        return 390 if day in week else 0
    monkeypatch.setattr(replay, "_count_rth_bars", fake_count)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(_assemble_continuous(
            None, "NQ", start_date=date(2026, 7, 22), context_days=2,
            forward_days=2, include_overnight=False,
            first_data_day=date(2026, 7, 20), cap_date=date(2026, 7, 24)))
    assert ei.value.status_code == 404


def test_assemble_continuous_eth_keeps_overnight_context(monkeypatch):
    week = [date(2026, 7, 21), date(2026, 7, 22)]
    monkeypatch.setattr(replay, "_fetch_bars",
                        make_fetch(week, include_overnight=True))

    async def fake_count(db, inst, day):
        return 0
    monkeypatch.setattr(replay, "_count_rth_bars", fake_count)

    payload = asyncio.run(_assemble_continuous(
        None, "NQ", start_date=date(2026, 7, 22), context_days=0,
        forward_days=1, include_overnight=True,
        first_data_day=date(2026, 7, 21), cap_date=date(2026, 7, 22)))

    # context_days=0 -> window opens at start-day session open (18:00 prior),
    # so overnight bars precede the playhead (the RTH open).
    assert payload["playhead_index"] > 0
    ph = payload["bars"][payload["playhead_index"]]
    assert ph["t"] == int(et(2026, 7, 22, 9, 30).timestamp())
    # every bar before the playhead is an overnight (non-RTH) bar
    pre = payload["bars"][:payload["playhead_index"]]
    assert pre and all(
        not _is_rth_bar(datetime.fromtimestamp(b["t"], timezone.utc))
        for b in pre)


# ── /continuous/more pagination continuity ───────────────────────────────

def test_more_continues_strictly_after_with_no_gap(monkeypatch):
    week = [date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24)]
    monkeypatch.setattr(replay, "_fetch_bars", make_fetch(week))

    async def fake_count(db, inst, day):
        return 390 if day in week else 0
    monkeypatch.setattr(replay, "_count_rth_bars", fake_count)

    # Initial window: start Wed 7/22, forward 1 -> Wed+Thu.
    first = asyncio.run(_assemble_continuous(
        None, "NQ", start_date=date(2026, 7, 22), context_days=0,
        forward_days=1, include_overnight=False,
        first_data_day=date(2026, 7, 22), cap_date=date(2026, 7, 24)))
    assert first["last_date"] == "2026-07-23"
    assert first["has_more_forward"] is True

    after_ts = first["bars"][-1]["t"]        # Thu 15:59
    nxt = asyncio.run(_assemble_more(
        None, "NQ", after_ts=after_ts, days=1, include_overnight=False,
        cap_date=date(2026, 7, 24)))

    assert nxt["bars"], "more should return the next day"
    # strictly after after_ts: no overlap
    assert nxt["bars"][0]["t"] > after_ts
    # contiguous next trading day, no gap: first more bar is Fri 09:30
    assert nxt["bars"][0]["t"] == int(et(2026, 7, 24, 9, 30).timestamp())
    assert nxt["day_starts"][0]["date"] == "2026-07-24"
    # concatenated series is strictly ascending
    joined = [b["t"] for b in first["bars"]] + [b["t"] for b in nxt["bars"]]
    assert all(joined[i] < joined[i + 1] for i in range(len(joined) - 1))
    # Fri is the last day within cap -> no more forward
    assert nxt["has_more_forward"] is False


def test_more_empty_when_at_end(monkeypatch):
    week = [date(2026, 7, 23), date(2026, 7, 24)]
    monkeypatch.setattr(replay, "_fetch_bars", make_fetch(week))

    async def fake_count(db, inst, day):
        return 390 if day in week else 0
    monkeypatch.setattr(replay, "_count_rth_bars", fake_count)

    after_ts = int(et(2026, 7, 24, 15, 59).timestamp())  # last bar of last day
    nxt = asyncio.run(_assemble_more(
        None, "NQ", after_ts=after_ts, days=5, include_overnight=False,
        cap_date=date(2026, 7, 24)))
    assert nxt["bars"] == []
    assert nxt["has_more_forward"] is False


def test_more_rejects_absurd_after_ts():
    with pytest.raises(HTTPException) as ei:
        asyncio.run(_assemble_more(
            None, "NQ", after_ts=10 ** 15, days=1, include_overnight=False,
            cap_date=date(2026, 7, 24)))
    assert ei.value.status_code == 422


# ── blind /continuous/random caps at yesterday ───────────────────────────

def test_continuous_random_caps_window_at_yesterday(monkeypatch):
    # Generate RTH bars for ANY weekday on demand so any picked window is
    # served; force randint to the MAX index so the pick lands at latest_start
    # (the tightest-against-the-cap case).
    async def fetch_any(db, instrument, start, end):
        out = []
        d = start.astimezone(ET).date()
        end_d = end.astimezone(ET).date()
        while d <= end_d:
            if d.weekday() < 5:
                for b in rth_bars(d):
                    if start <= b[0] < end:
                        out.append(b)
            d = d + timedelta(days=1)
        out.sort(key=lambda b: b[0])
        return out
    monkeypatch.setattr(replay, "_fetch_bars", fetch_any)

    async def fake_count(db, inst, day):
        return 390 if day.weekday() < 5 else 0
    monkeypatch.setattr(replay, "_count_rth_bars", fake_count)

    first_ts = datetime(2023, 5, 1, 13, 30, tzinfo=timezone.utc)
    last_ts = datetime.now(timezone.utc)

    async def fake_range(db, inst):
        return first_ts, last_ts
    monkeypatch.setattr(replay, "_data_range", fake_range)
    monkeypatch.setattr(replay.random, "randint", lambda a, b: b)  # max start

    payload = asyncio.run(replay.replay_continuous_random(
        instrument="NQ", context_days=5, forward_days=20, eth=0,
        current_user=None, db=None))

    yesterday = datetime.now(ET).date() - timedelta(days=1)
    assert payload["blind"] is True
    assert date.fromisoformat(payload["start_date"]) <= yesterday
    # the whole forward window stays on or before the last complete day
    assert date.fromisoformat(payload["last_date"]) <= yesterday
    assert payload["playhead_index"] >= 0
