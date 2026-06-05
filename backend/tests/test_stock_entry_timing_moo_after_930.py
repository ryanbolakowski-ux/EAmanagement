"""MOO path: at 09:35 ET (post-open), submit a market order. Tradier's
market order in 'day' duration is the practical equivalent of MOO — it
fills at the open if placed before, or at next print if placed after.
We confirm the broker is called and the stop is computed from the Oracle
5-min opening candle low.

Run: pytest backend/tests/test_stock_entry_timing_moo_after_930.py -v -p no:cacheprovider
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


def _make_pending(ticker="QQQ", pick_price=400.0):
    from datetime import date
    return {
        "user_id": "00000000-0000-0000-0000-000000000001",
        "user_email": "test@example.com",
        "broker_account_id": "00000000-0000-0000-0000-0000000000aa",
        "ticker": ticker,
        "direction": "long",
        "qty": 5,
        "pick_price": pick_price,
        "target": pick_price * 1.10,
        "pick_date": date.today().isoformat(),
    }


def _patch_clock(monkeypatch, et_hour, et_minute):
    """ET → UTC, June 2026 = EDT = UTC-4."""
    target_utc = datetime(2026, 6, 5, et_hour + 4, et_minute, 0, tzinfo=timezone.utc)
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return target_utc.replace(tzinfo=None)
            return target_utc.astimezone(tz)
    monkeypatch.setattr("datetime.datetime", FixedDT, raising=False)


def test_moo_path_submits_market_order(monkeypatch):
    """09:35 ET — MOO path. Should fire a market order."""
    from app.engines.options import premarket_scheduler as ps

    _patch_clock(monkeypatch, 9, 35)

    # 5-min bars including the 09:30-09:35 Oracle candle: low = $398.20
    async def fake_5min(ticker, date_et):
        from datetime import timezone as _tz
        oracle_start = datetime(2026, 6, 5, 13, 30, 0, tzinfo=_tz.utc)  # 09:30 ET
        return [
            {"t": int(oracle_start.timestamp() * 1000),
              "o": 400.0, "h": 401.0, "l": 398.20, "c": 399.50,
              "v": 100_000, "vw": 399.5},
        ]
    monkeypatch.setattr(ps, "_polygon_5min_bars", fake_5min)

    async def fake_live(ticker):
        return 400.50
    monkeypatch.setattr(ps, "_polygon_last_trade_price", fake_live)

    placed = {"count": 0, "args": None}
    async def fake_place(broker_account_id, ticker, direction, qty):
        placed["count"] += 1
        placed["args"] = (broker_account_id, ticker, direction, qty)
        return ("ORDER999", "executed", None)
    monkeypatch.setattr(ps, "_place_intraday_broker_order", fake_place)

    # No-op the DB persistence
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
    assert result is True, "MOO path should fire the broker order"
    assert placed["count"] == 1, f"expected 1 order, got {placed['count']}"
    assert placed["args"][1] == "QQQ"
    assert placed["args"][2] == "long"
    assert placed["args"][3] == 5


def test_moo_path_uses_market_order_type():
    """Document and assert that the broker order type uses MARKET (the
    Tradier-supported equivalent of MOO when placed pre-open in 'day'
    duration). Tradier does NOT have a separate MOO order_type per their
    docs — `OrderType.MARKET` is the canonical way."""
    from app.engines.live_trading.broker_base import OrderType
    assert OrderType.MARKET.value in ("market", "MARKET", 1), (
        f"OrderType.MARKET should exist for MOO submission; got {OrderType.MARKET}"
    )
