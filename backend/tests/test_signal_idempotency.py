"""Bug 4 + Bug 5 — repeated scanner ticks must NOT resend; delivery columns set.

Calls _emit_signal directly (channels=[] so no real email) and asserts that
three identical emits create exactly one row with duplicate_suppressed_count=2.

Each async block runs in a dedicated thread with its own event loop and disposes
the shared engine at both ends, so no asyncpg connection ever survives a closed
loop to poison later tests.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy import text
from app.database import async_session_factory, engine
from app.engines.account_signals import runner
from app.engines.account_signals.signal_guard import make_idempotency_key


def _run_isolated(coro_factory):
    """Run an async coroutine in a fresh thread+loop, disposing the shared
    engine before and after so connections never cross loops."""
    result = {}

    def worker():
        async def wrapped():
            await engine.dispose()
            try:
                return await coro_factory()
            finally:
                await engine.dispose()
        result["value"] = asyncio.run(wrapped())

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    return result.get("value")


class _FakeSig:
    class _S:
        value = "long"
    signal = _S()
    entry_price = 30043.5
    stop_loss = 30033.5
    take_profit = 30073.5
    timestamp = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    metadata = {"bias": "bullish"}


def _watcher_fks():
    async def go():
        async with async_session_factory() as db:
            row = (await db.execute(text(
                "SELECT w.id, w.strategy_id, w.user_id FROM account_signal_watchers w LIMIT 1"
            ))).fetchone()
        return row
    return _run_isolated(go)


def test_repeated_emit_dedupes():
    row = _watcher_fks()
    if not row:
        pytest.skip("no watcher available to attach FKs")
    wid, sid, uid = str(row[0]), str(row[1]), str(row[2])
    key = make_idempotency_key(wid, sid, "NQ", "long",
                               _FakeSig.timestamp, 30043.5, 30033.5, 30073.5)

    async def go():
        async with async_session_factory() as db:
            await db.execute(text("DELETE FROM account_signals WHERE idempotency_key=:k"), {"k": key})
            await db.commit()
        for _ in range(3):
            await runner._emit_signal(wid, sid, uid, "TEST", [], "idem-test", "NQ",
                                      _FakeSig(), "noone@example.test", "tester")
        async with async_session_factory() as db:
            n = (await db.execute(text("SELECT count(*) FROM account_signals WHERE idempotency_key=:k"), {"k": key})).scalar()
            sup = (await db.execute(text("SELECT duplicate_suppressed_count FROM account_signals WHERE idempotency_key=:k"), {"k": key})).scalar()
            detected = (await db.execute(text("SELECT detected_at FROM account_signals WHERE idempotency_key=:k"), {"k": key})).scalar()
            await db.execute(text("DELETE FROM account_signals WHERE idempotency_key=:k"), {"k": key})
            await db.commit()
        return n, sup, detected

    n, sup, detected = _run_isolated(go)
    assert n == 1, f"expected exactly 1 row, got {n}"
    assert sup == 2, f"expected 2 suppressed, got {sup}"
    assert detected is not None, "detected_at should be populated"


def test_invalid_geometry_emit_creates_no_row():
    """Bug 6 enforced in the emit path: a bad-geometry signal is never persisted."""
    class _Bad(_FakeSig):
        class _S:
            value = "long"
        signal = _S()
        stop_loss = 30099.0  # stop ABOVE entry on a long -> invalid
        entry_price = 30043.5
        take_profit = 30073.5
        timestamp = datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc)

    row = _watcher_fks()
    if not row:
        pytest.skip("no watcher available")
    wid, sid, uid = str(row[0]), str(row[1]), str(row[2])
    key = make_idempotency_key(wid, sid, "NQ", "long", _Bad.timestamp, 30043.5, 30099.0, 30073.5)

    async def go():
        await runner._emit_signal(wid, sid, uid, "TEST", [], "bad-geo", "NQ",
                                  _Bad(), "noone@example.test", "tester")
        async with async_session_factory() as db:
            n = (await db.execute(text("SELECT count(*) FROM account_signals WHERE idempotency_key=:k"), {"k": key})).scalar()
        return n

    assert _run_isolated(go) == 0
