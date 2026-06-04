"""BUG A: when the trail watcher exits a position, BOTH
open_positions_watch AND trades must be updated to status='closed'.

Until 2026-06-04 only the sidecar was updated, leaving trades stuck open
(URG, AIIO for jaceford12). This test verifies the mirror UPDATE fires.

Run: pytest backend/tests/test_trail_watcher_updates_trades.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import types
from unittest.mock import patch, AsyncMock, MagicMock

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
        # Only the SELECT FROM open_positions_watch returns rows, so we
        # only return our seeded rows for that path. UPDATEs return empty.
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


def _watch_row(user_id="80a5070b-61db-49e2-9915-eecce0c123b2",
               ticker="URG", qty=476, entry=2.10, trail_pct=3.0, hard_stop=2.00,
               trail_high=2.10):
    """Mimic the row returned by SELECT FROM open_positions_watch."""
    return types.SimpleNamespace(
        id="watch-1",
        user_id=user_id,
        broker_account_id="ba-1",
        ticker=ticker,
        qty=qty,
        entry_price=entry,
        trail_pct=trail_pct,
        trail_high=trail_high,
        hard_stop=hard_stop,
        target=None,
    )


def _broker_acct():
    return types.SimpleNamespace(id="ba-1", account_id="ACC1")


class _FakeResp:
    def __init__(self, code=200, body=None):
        self.status_code = code
        self._body = body or {}
    def json(self):
        return self._body


def test_trail_watch_updates_trades_table(monkeypatch):
    """Hard-stop hit: price falls below hard_stop. Mock the broker SELL,
    then assert that BOTH the UPDATE open_positions_watch AND the
    UPDATE trades fire with the right params."""
    from app.engines.options import premarket_scheduler as ps

    db = FakeAsyncSession(rows=[_watch_row()], acct=_broker_acct())
    monkeypatch.setattr("app.database.async_session_factory", lambda: db)

    # Polygon: return a price BELOW the hard_stop ($2.00) so the watcher exits
    def _fake_get(url, params=None, timeout=None):
        return _FakeResp(200, {"ticker": {"lastTrade": {"p": 1.98}}})
    monkeypatch.setattr("requests.get", _fake_get)
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")

    fake_broker = MagicMock()
    fake_broker.connect = AsyncMock(return_value=True)
    fake_broker.place_order = AsyncMock(return_value=types.SimpleNamespace(
        broker_order_id="ord_42",
        filled_price=1.98,
        status="filled",
    ))
    monkeypatch.setattr(
        "app.engines.live_trading.broker_factory.build_broker_from_account",
        lambda acct: fake_broker,
    )

    _run(ps._run_trailing_stop_watcher())

    # 1) broker.place_order was called - we tried to sell
    assert fake_broker.place_order.call_count == 1, "expected 1 SELL order"

    # 2) UPDATE open_positions_watch fired with the exit row
    watch_updates = [c for c in db.calls
                     if "UPDATE open_positions_watch" in c[0]
                     and "status='closed'" in c[0]]
    assert len(watch_updates) == 1, (
        f"expected 1 open_positions_watch close UPDATE; got {len(watch_updates)}; "
        f"calls={[c[0][:90] for c in db.calls]}"
    )

    # 3) UPDATE trades fired with the same exit_price, user_id, instrument
    trade_updates = [c for c in db.calls
                     if "UPDATE trades" in c[0]
                     and "status = 'closed'" in c[0]]
    assert len(trade_updates) == 1, (
        f"BUG A REGRESSION: expected 1 trades close UPDATE; got {len(trade_updates)}; "
        f"calls={[c[0][:90] for c in db.calls]}"
    )

    params = trade_updates[0][1]
    assert params["p"] == 1.98, f"exit_price should be 1.98, got {params['p']!r}"
    assert params["sym"] == "URG", f"instrument should be URG, got {params['sym']!r}"
    assert params["uid"] == "80a5070b-61db-49e2-9915-eecce0c123b2"
    assert params["oid"] == "ord_42"
