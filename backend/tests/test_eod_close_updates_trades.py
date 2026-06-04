"""BUG A: when the 15:55 ET EOD close exits a position, BOTH
open_positions_watch AND trades must be updated to status='closed'.

Same pattern as test_eod_close.py but specifically asserting the trades
mirror UPDATE fires.

Run: pytest backend/tests/test_eod_close_updates_trades.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import types
from datetime import time as dtime
from unittest.mock import AsyncMock, MagicMock

import pytest


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows or []
    def fetchall(self):
        return self._rows
    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None
    def first(self):
        return self._rows[0] if self._rows else None


class FakeAsyncSession:
    def __init__(self, rows=None, acct=None):
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self._rows = rows or []
        self._acct = acct
    async def execute(self, stmt, params=None):
        sql = str(getattr(stmt, "text", stmt))
        self.calls.append((sql, params or {}))
        if "BrokerAccount" in sql or "broker_accounts" in sql:
            return _FakeResult([self._acct] if self._acct else [])
        if "FROM open_positions_watch" in sql:
            return _FakeResult(self._rows)
        return _FakeResult([])
    async def commit(self):
        self.commits += 1
    async def rollback(self):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return None


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_et(hour=15, minute=57, weekday=1, date_iso="2026-06-04"):
    return types.SimpleNamespace(
        time=lambda: dtime(hour, minute),
        weekday=lambda: weekday,
        date=lambda: types.SimpleNamespace(isoformat=lambda: date_iso),
    )


def _watch_row(ticker="AIIO", qty=342, entry=2.92,
               user_id="80a5070b-61db-49e2-9915-eecce0c123b2"):
    return types.SimpleNamespace(
        id="watch-eod-1",
        user_id=user_id,
        broker_account_id="ba-1",
        ticker=ticker,
        qty=qty,
        entry_price=entry,
    )


@pytest.fixture(autouse=True)
def _reset_marker():
    from app.engines.options import premarket_scheduler as ps
    ps._eod_fired_for_date.clear()
    yield
    ps._eod_fired_for_date.clear()


def test_eod_close_updates_trades_table(monkeypatch):
    """When EOD fires inside the 15:55 ET window, the trades mirror
    UPDATE must run alongside the open_positions_watch UPDATE."""
    from app.engines.options import premarket_scheduler as ps

    rows = [_watch_row()]
    db = FakeAsyncSession(rows=rows, acct=types.SimpleNamespace(id="ba-1", account_id="ACC1"))

    fake_broker = MagicMock()
    fake_broker.connect = AsyncMock(return_value=True)
    fake_broker.place_order = AsyncMock(return_value=types.SimpleNamespace(
        broker_order_id="ord_eod_1",
        filled_price=2.76,
        status="filled",
    ))

    monkeypatch.setattr(ps, "_eod_now_et", lambda: _fake_et())
    monkeypatch.setattr("app.database.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.engines.live_trading.broker_factory.build_broker_from_account",
        lambda acct: fake_broker,
    )

    _run(ps._check_end_of_day_close())

    # broker SELL placed
    assert fake_broker.place_order.call_count == 1

    # watch UPDATE
    watch_updates = [c for c in db.calls
                     if "UPDATE open_positions_watch" in c[0]
                     and "eod_auto_close" in c[0]]
    assert len(watch_updates) == 1, "expected 1 watch close UPDATE"

    # trades UPDATE - this is what BUG A added
    trade_updates = [c for c in db.calls
                     if "UPDATE trades" in c[0]
                     and "eod_auto_close" in c[0]]
    assert len(trade_updates) == 1, (
        f"BUG A REGRESSION: expected 1 trades close UPDATE in EOD path; "
        f"got {len(trade_updates)}; calls={[c[0][:90] for c in db.calls]}"
    )

    params = trade_updates[0][1]
    assert params["px"] == 2.76, f"exit_price should be 2.76, got {params['px']!r}"
    assert params["sym"] == "AIIO"
    assert params["uid"] == "80a5070b-61db-49e2-9915-eecce0c123b2"
    assert params["oid"] == "ord_eod_1"
