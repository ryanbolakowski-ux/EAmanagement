"""Issue 2 verification — _check_end_of_day_close() must:
  1. Be a no-op outside the 15:55-16:00 ET window
  2. At 15:55 ET, iterate open positions and submit market-SELL via the broker
  3. Be idempotent: second call on same ET date is a no-op
  4. Update each row to status='closed', exit_reason='eod_auto_close'

Also covers the trail-watcher visibility log: the function must log
"[TrailWatch] checking N rows" so prod can be observed.

Run: pytest backend/tests/test_eod_close.py -v -p no:cacheprovider
"""
import asyncio
import types
from datetime import time as dtime
from unittest.mock import patch, AsyncMock, MagicMock

import pytest


class _FakeResult:
    def __init__(self, rows): self._rows = rows or []
    def fetchall(self): return self._rows
    def scalar_one_or_none(self): return self._rows[0] if self._rows else None
    def first(self): return self._rows[0] if self._rows else None


class FakeAsyncSession:
    def __init__(self, rows=None, acct=None):
        self.calls = []
        self.commits = 0
        self._rows = rows or []
        self._acct = acct
    async def execute(self, stmt, params=None):
        sql = str(getattr(stmt, "text", stmt))
        self.calls.append((sql, params or {}))
        # SELECT broker account
        if "BrokerAccount" in sql or "broker_accounts" in sql:
            return _FakeResult([self._acct] if self._acct else [])
        return _FakeResult(self._rows)
    async def commit(self): self.commits += 1
    async def rollback(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None


@pytest.fixture(autouse=True)
def _reset_marker():
    from app.engines.options import premarket_scheduler as ps
    ps._eod_fired_for_date.clear()
    yield
    ps._eod_fired_for_date.clear()


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _fake_et(hour=15, minute=57, weekday=1, date_iso="2026-06-02"):
    return types.SimpleNamespace(
        time=lambda: dtime(hour, minute),
        weekday=lambda: weekday,
        date=lambda: types.SimpleNamespace(isoformat=lambda: date_iso),
    )


def _mk_row(id_="row-1", ticker="AIIO", qty=100):
    return types.SimpleNamespace(
        id=id_,
        user_id="u-jaceford",
        broker_account_id="ba-1",
        ticker=ticker,
        qty=qty,
        entry_price=3.60,
    )


def _mk_acct():
    return types.SimpleNamespace(id="ba-1", account_id="ACC1")


# ─── Test 1: no-op outside window ───
def test_eod_close_no_op_outside_window(monkeypatch):
    from app.engines.options import premarket_scheduler as ps
    db = FakeAsyncSession(rows=[_mk_row()])

    monkeypatch.setattr(ps, "_eod_now_et", lambda: _fake_et(hour=12, minute=0))
    monkeypatch.setattr("app.database.async_session_factory", lambda: db)

    _run(ps._check_end_of_day_close())

    # No place_order, no commits
    assert db.commits == 0
    assert not [c for c in db.calls if "open_positions_watch" in c[0]]


# ─── Test 2: fires inside window, calls place_order, updates rows ───
def test_eod_close_fires_inside_window_and_updates_rows(monkeypatch):
    from app.engines.options import premarket_scheduler as ps
    rows = [_mk_row(id_=f"r-{i}", ticker=t)
            for i, t in enumerate(["AIIO", "EEIQ"])]
    db = FakeAsyncSession(rows=rows, acct=_mk_acct())

    fake_broker = MagicMock()
    fake_broker.connect = AsyncMock(return_value=True)
    fake_broker.place_order = AsyncMock(return_value=types.SimpleNamespace(
        broker_order_id="ord_123", filled_price=3.10, status="filled",
    ))

    monkeypatch.setattr(ps, "_eod_now_et", lambda: _fake_et(hour=15, minute=57))
    monkeypatch.setattr("app.database.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.engines.live_trading.broker_factory.build_broker_from_account",
        lambda acct: fake_broker,
    )

    _run(ps._check_end_of_day_close())

    # Verify sell orders were placed for both rows
    assert fake_broker.place_order.call_count == 2, (
        f"expected 2 sell orders, got {fake_broker.place_order.call_count}"
    )
    # Verify closed updates issued
    close_updates = [c for c in db.calls if "status='closed'" in c[0]
                                              and "eod_auto_close" in c[0]]
    assert len(close_updates) == 2, (
        f"expected 2 close updates; got {len(close_updates)}; "
        f"calls={[c[0][:80] for c in db.calls]}"
    )


# ─── Test 3: idempotent ───
def test_eod_close_is_idempotent(monkeypatch):
    from app.engines.options import premarket_scheduler as ps
    db = FakeAsyncSession(rows=[_mk_row()], acct=_mk_acct())

    fake_broker = MagicMock()
    fake_broker.connect = AsyncMock(return_value=True)
    fake_broker.place_order = AsyncMock(return_value=types.SimpleNamespace(
        broker_order_id="ord_1", filled_price=3.0, status="filled",
    ))

    monkeypatch.setattr(ps, "_eod_now_et", lambda: _fake_et(hour=15, minute=57))
    monkeypatch.setattr("app.database.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.engines.live_trading.broker_factory.build_broker_from_account",
        lambda acct: fake_broker,
    )

    _run(ps._check_end_of_day_close())
    first_count = fake_broker.place_order.call_count
    _run(ps._check_end_of_day_close())
    second_count = fake_broker.place_order.call_count

    assert first_count == 1
    assert second_count == 1, (
        f"second call should be no-op; place_order ran {second_count}x"
    )


# ─── Test 4: weekend is no-op ───
def test_eod_close_skips_weekend(monkeypatch):
    from app.engines.options import premarket_scheduler as ps
    db = FakeAsyncSession(rows=[_mk_row()], acct=_mk_acct())

    monkeypatch.setattr(ps, "_eod_now_et",
                        lambda: _fake_et(hour=15, minute=57, weekday=5))  # Saturday
    monkeypatch.setattr("app.database.async_session_factory", lambda: db)

    _run(ps._check_end_of_day_close())
    assert db.commits == 0


# ─── Test 5: trail-watcher visibility log fires unconditionally ───
def test_trail_watcher_logs_row_count(monkeypatch, caplog):
    """The visibility log line `[TrailWatch] checking N rows` must fire
    whenever _run_trailing_stop_watcher runs (regardless of whether there
    are any rows to process). This is how we know it's not orphaned."""
    import logging
    from app.engines.options import premarket_scheduler as ps

    # Empty rows: still has to log "checking 0 open positions"
    db = FakeAsyncSession(rows=[])
    monkeypatch.setenv("POLYGON_API_KEY", "test")
    monkeypatch.setattr("app.database.async_session_factory", lambda: db)

    # Capture loguru output by intercepting it
    captured = []
    from loguru import logger as _lg
    _id = _lg.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        _run(ps._run_trailing_stop_watcher())
    finally:
        _lg.remove(_id)

    joined = " ".join(captured)
    assert "[TrailWatch] checking" in joined, (
        f"expected '[TrailWatch] checking' log; got: {joined[:400]}"
    )
