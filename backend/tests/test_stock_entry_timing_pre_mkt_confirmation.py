"""Pre-market entry-timing gate: between 08:30-09:25 ET we ENTER only when
both (a) live price > pre-market VWAP and (b) latest closed 5-min pre-mkt
bar high > the bar before it. If either fails: WAIT.

Run: pytest backend/tests/test_stock_entry_timing_pre_mkt_confirmation.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_pending(ticker="TSLA"):
    from datetime import date
    return {
        "user_id": "00000000-0000-0000-0000-000000000001",
        "user_email": "test@example.com",
        "broker_account_id": "00000000-0000-0000-0000-0000000000aa",
        "ticker": ticker,
        "direction": "long",
        "qty": 10,
        "pick_price": 100.00,
        "target": 110.00,
        "pick_date": date.today().isoformat(),
    }


def _et_to_utc(year, month, day, et_hour, et_minute):
    """Convert ET (EDT in June, UTC-4) to UTC datetime."""
    return datetime(year, month, day, et_hour + 4, et_minute, 0, tzinfo=timezone.utc)


def _patch_clock(monkeypatch, et_hour: int, et_minute: int):
    """Pin datetime.now() to a specific ET clock during the test."""
    target_utc = _et_to_utc(2026, 6, 5, et_hour, et_minute)
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return target_utc.replace(tzinfo=None)
            return target_utc.astimezone(tz)
    monkeypatch.setattr("datetime.datetime", FixedDT, raising=False)
    return target_utc


def test_pre_mkt_window_enters_when_vwap_and_higher_high_pass(monkeypatch):
    """09:00 ET, live price ABOVE vwap, latest pre-mkt 5-min bar high
    GREATER than previous. Should call the broker placement."""
    from app.engines.options import premarket_scheduler as ps

    _patch_clock(monkeypatch, 9, 0)

    # Mock pre-market 1-min bars: simple uptrend, vwap ~ $99
    async def fake_1min(ticker, date_et):
        # bars timestamped throughout pre-market — give them all volume
        from datetime import timezone as _tz
        bars = []
        # Pre-mkt: 04:00 (UTC 08:00) onwards
        for hh, mm, px, vol in [(8, 0, 98.0, 1000), (8, 30, 98.5, 2000),
                                 (9, 0, 99.0, 3000), (9, 30, 99.5, 4000),
                                 (10, 0, 99.5, 5000), (11, 0, 99.5, 6000),
                                 (12, 0, 99.5, 5000), (13, 0, 99.5, 4000)]:
            t = datetime(2026, 6, 5, hh, mm, 0, tzinfo=_tz.utc)
            bars.append({"t": int(t.timestamp() * 1000), "o": px, "h": px + 0.5,
                          "l": px - 0.3, "c": px, "v": vol, "vw": px})
        return bars
    monkeypatch.setattr(ps, "_polygon_1min_bars", fake_1min)

    # 5-min bars with higher-high: each subsequent pre-mkt bar high higher
    async def fake_5min(ticker, date_et):
        from datetime import timezone as _tz
        bars = []
        et_starts = [
            (4, 0, 98.0, 98.5, 97.8, 98.4),     # 04:00 ET (08:00 UTC)
            (4, 5, 98.4, 98.7, 98.3, 98.6),
            # ... skip ahead to the latest two before 09:00 ET
            (8, 30, 98.8, 99.0, 98.7, 98.95),  # bar -2 high = 99.00
            (8, 35, 98.95, 99.20, 98.9, 99.10),  # bar -1 high = 99.20 > 99.00 → HH
        ]
        for hh, mm, o, h, l, c in et_starts:
            t = datetime(2026, 6, 5, hh + 4, mm, 0, tzinfo=_tz.utc)  # ET → UTC
            bars.append({"t": int(t.timestamp() * 1000), "o": o, "h": h,
                          "l": l, "c": c, "v": 1000, "vw": (h + l + c) / 3})
        return bars
    monkeypatch.setattr(ps, "_polygon_5min_bars", fake_5min)

    # Live price ABOVE vwap (vwap ~ 98.5, live = 99.5)
    async def fake_live(ticker):
        return 99.5
    monkeypatch.setattr(ps, "_polygon_last_trade_price", fake_live)

    placed = {"count": 0, "args": None}
    async def fake_place(broker_account_id, ticker, direction, qty):
        placed["count"] += 1
        placed["args"] = (broker_account_id, ticker, direction, qty)
        return ("ORDER123", "executed", None)
    monkeypatch.setattr(ps, "_place_intraday_broker_order", fake_place)

    # No-op the DB persistence (we only care about whether the broker was called)
    class _FakeDB:
        async def execute(self, *a, **k): return _FakeResult()
        async def commit(self): return None
    class _FakeResult:
        def first(self): return None
        def scalar(self): return "00000000-0000-0000-0000-0000000000bb"
    class _FakeAS:
        async def __aenter__(self): return _FakeDB()
        async def __aexit__(self, *a): return None
    monkeypatch.setattr("app.database.async_session_factory", lambda: _FakeAS())

    async def _fake_clear(*a, **k): return None
    monkeypatch.setattr(ps, "_clear_pending_entry", _fake_clear)

    result = _run(ps._execute_stock_pick_with_timing_gate(_make_pending()))
    assert result is True, "expected timing-gate to ENTER (both vwap+HH pass)"
    assert placed["count"] == 1, f"broker order should have fired exactly once, got {placed['count']}"


def test_pre_mkt_window_waits_when_vwap_fails(monkeypatch):
    """Live price BELOW pre-mkt vwap → WAIT, do not place order."""
    from app.engines.options import premarket_scheduler as ps

    _patch_clock(monkeypatch, 9, 0)

    async def fake_1min(ticker, date_et):
        from datetime import timezone as _tz
        t = datetime(2026, 6, 5, 12, 0, 0, tzinfo=_tz.utc)
        return [{"t": int(t.timestamp() * 1000), "o": 100, "h": 100.5,
                  "l": 99.5, "c": 100, "v": 10_000, "vw": 100.0}]
    monkeypatch.setattr(ps, "_polygon_1min_bars", fake_1min)

    async def fake_5min(ticker, date_et):
        from datetime import timezone as _tz
        return [
            {"t": int(datetime(2026, 6, 5, 12, 30, 0, tzinfo=_tz.utc).timestamp() * 1000),
              "o": 100, "h": 100.5, "l": 99.5, "c": 100, "v": 1000, "vw": 100.0},
            {"t": int(datetime(2026, 6, 5, 12, 35, 0, tzinfo=_tz.utc).timestamp() * 1000),
              "o": 100, "h": 101.0, "l": 99.5, "c": 100.5, "v": 1000, "vw": 100.0},
        ]
    monkeypatch.setattr(ps, "_polygon_5min_bars", fake_5min)

    async def fake_live(ticker):
        return 98.0  # below vwap
    monkeypatch.setattr(ps, "_polygon_last_trade_price", fake_live)

    placed = {"count": 0}
    async def fake_place(*a, **k):
        placed["count"] += 1
        return ("X", "executed", None)
    monkeypatch.setattr(ps, "_place_intraday_broker_order", fake_place)

    result = _run(ps._execute_stock_pick_with_timing_gate(_make_pending()))
    assert result is False, "vwap_fail should NOT enter"
    assert placed["count"] == 0, "broker order should not fire on vwap fail"


def test_pre_mkt_window_waits_when_no_higher_high(monkeypatch):
    """Latest pre-mkt 5-min bar high <= previous → WAIT."""
    from app.engines.options import premarket_scheduler as ps

    _patch_clock(monkeypatch, 9, 0)

    async def fake_1min(ticker, date_et):
        from datetime import timezone as _tz
        t = datetime(2026, 6, 5, 12, 0, 0, tzinfo=_tz.utc)
        return [{"t": int(t.timestamp() * 1000), "o": 100, "h": 100.5,
                  "l": 99.5, "c": 100, "v": 10_000, "vw": 100.0}]
    monkeypatch.setattr(ps, "_polygon_1min_bars", fake_1min)

    # 5-min bars with NO higher high (latest bar high == previous)
    async def fake_5min(ticker, date_et):
        from datetime import timezone as _tz
        return [
            {"t": int(datetime(2026, 6, 5, 12, 30, 0, tzinfo=_tz.utc).timestamp() * 1000),
              "o": 100, "h": 101.0, "l": 99.5, "c": 100.5, "v": 1000, "vw": 100.0},
            {"t": int(datetime(2026, 6, 5, 12, 35, 0, tzinfo=_tz.utc).timestamp() * 1000),
              "o": 100, "h": 101.0, "l": 99.5, "c": 100.5, "v": 1000, "vw": 100.0},
        ]
    monkeypatch.setattr(ps, "_polygon_5min_bars", fake_5min)

    async def fake_live(ticker):
        return 100.5
    monkeypatch.setattr(ps, "_polygon_last_trade_price", fake_live)

    placed = {"count": 0}
    async def fake_place(*a, **k):
        placed["count"] += 1
        return ("X", "executed", None)
    monkeypatch.setattr(ps, "_place_intraday_broker_order", fake_place)

    result = _run(ps._execute_stock_pick_with_timing_gate(_make_pending()))
    assert result is False, "no higher-high should NOT enter"
    assert placed["count"] == 0


def test_before_830_et_defers(monkeypatch):
    """08:15 ET — should DEFER (too early)."""
    from app.engines.options import premarket_scheduler as ps
    _patch_clock(monkeypatch, 8, 15)

    placed = {"count": 0}
    async def fake_place(*a, **k):
        placed["count"] += 1
        return ("X", "executed", None)
    monkeypatch.setattr(ps, "_place_intraday_broker_order", fake_place)

    result = _run(ps._execute_stock_pick_with_timing_gate(_make_pending()))
    assert result is False
    assert placed["count"] == 0
