"""Bug 4 + Bug 5 — repeated scanner ticks must NOT resend; delivery columns set.

Calls _emit_signal directly (channels=[] so no real email) and asserts that
three identical emits create exactly one row with duplicate_suppressed_count=2.
"""
import asyncio
import uuid
from datetime import datetime, timezone
import pytest
from sqlalchemy import text
from app.database import async_session_factory, engine
from app.engines.account_signals import runner
from app.engines.account_signals.signal_guard import make_idempotency_key


class _FakeSig:
    class _S:
        value = "long"
    signal = _S()
    entry_price = 30043.5
    stop_loss = 30033.5
    take_profit = 30073.5
    timestamp = datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc)
    metadata = {"bias": "bullish"}


def test_repeated_emit_dedupes():
    async def go():
        await engine.dispose()  # rebind pool to this asyncio.run loop
        async with async_session_factory() as db:
            row = (await db.execute(text(
                "SELECT w.id, w.strategy_id, w.user_id FROM account_signal_watchers w LIMIT 1"
            ))).fetchone()
        if not row:
            pytest.skip("no watcher available to attach FKs")
        wid, sid, uid = str(row[0]), str(row[1]), str(row[2])
        key = make_idempotency_key(wid, sid, "NQ", "long",
                                   _FakeSig.timestamp, 30043.5, 30033.5, 30073.5)
        # clean any prior leftovers
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
    n, sup, detected = asyncio.run(go())
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

    async def go():
        await engine.dispose()  # rebind pool to this asyncio.run loop
        async with async_session_factory() as db:
            row = (await db.execute(text(
                "SELECT w.id, w.strategy_id, w.user_id FROM account_signal_watchers w LIMIT 1"))).fetchone()
        if not row:
            pytest.skip("no watcher available")
        wid, sid, uid = str(row[0]), str(row[1]), str(row[2])
        key = make_idempotency_key(wid, sid, "NQ", "long", _Bad.timestamp, 30043.5, 30099.0, 30073.5)
        await runner._emit_signal(wid, sid, uid, "TEST", [], "bad-geo", "NQ",
                                  _Bad(), "noone@example.test", "tester")
        async with async_session_factory() as db:
            n = (await db.execute(text("SELECT count(*) FROM account_signals WHERE idempotency_key=:k"), {"k": key})).scalar()
        return n
    assert asyncio.run(go()) == 0
