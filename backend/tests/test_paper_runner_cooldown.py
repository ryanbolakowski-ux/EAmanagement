"""Guard-level tests for the paper-runner overtrade rules.

These hit the live test DB to exercise the cooldown + max-trades counts
(both require querying `trades`). Open-position checks use the in-memory
snapshot parameter so they're DB-free.

Each test makes its own throwaway session_id and strategy and cleans up,
following the same isolated-loop pattern as test_signal_idempotency.py.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import text

from app.database import async_session_factory, engine
from app.engines.entry_guard import (
    can_enter, ensure_strategy_columns, Decision,
)


def _run(coro_factory):
    out = {}
    def worker():
        async def wrap():
            await engine.dispose()
            try:
                return await coro_factory()
            finally:
                await engine.dispose()
        out["v"] = asyncio.run(wrap())
    t = threading.Thread(target=worker); t.start(); t.join()
    return out.get("v")


def _make_strategy(cooldown_min=5, max_trades_per_day=10, max_open_positions=1):
    """Create a throwaway strategy with the given limits. Returns its id."""
    async def go():
        await ensure_strategy_columns()
        sid = str(uuid.uuid4())
        async with async_session_factory() as db:
            uid_row = (await db.execute(text(
                "SELECT id FROM users LIMIT 1"
            ))).fetchone()
            uid = str(uid_row[0])
            await db.execute(text("""
                INSERT INTO strategies
                    (id, user_id, name, status, instruments,
                     primary_timeframe, execution_timeframe, higher_timeframes,
                     risk_reward_ratio, stop_loss_type, max_contracts, session_filters,
                     fvg_min_size_ticks, rule_tree, starred, kill_switch_enabled,
                     cooldown_min, max_trades_per_day, max_open_positions)
                VALUES (:id, :uid, :nm, 'active', '[]'::json,
                        '15m', '1m', '[]'::json,
                        2.0, 'structure', 1, '[]'::json,
                        4, '{}'::json, FALSE, TRUE,
                        :cd, :mt, :mo)
            """), {"id": sid, "uid": uid, "nm": f"test-cooldown-{sid[:8]}",
                    "cd": cooldown_min, "mt": max_trades_per_day, "mo": max_open_positions})
            await db.commit()
        return sid, uid
    return _run(go)


def _make_session(uid):
    async def go():
        sid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO trade_sessions (id, user_id, mode, is_active, started_at)
                VALUES (:id, :uid, 'paper', TRUE, NOW())
            """), {"id": sid, "uid": uid})
            await db.commit()
        return sid
    return _run(go)


def _record_entry(session_id, strategy_id, user_id, instrument, when=None):
    async def go():
        tid = str(uuid.uuid4())
        ts = when or datetime.now(timezone.utc)
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO trades
                    (id, strategy_id, user_id, session_id, mode, status,
                     instrument, direction, contracts, entry_price, stop_loss,
                     take_profit, entry_time, pnl, commission, net_pnl)
                VALUES (:id, :sid, :uid, :sess, 'paper', 'closed',
                        :inst, 'long', 1, 100.0, 99.0,
                        102.0, :t, 0, 0, 0)
            """), {"id": tid, "sid": strategy_id, "uid": user_id, "sess": session_id,
                    "inst": instrument, "t": ts})
            await db.commit()
        return tid
    return _run(go)


def _cleanup(strategy_id, session_id):
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("DELETE FROM trades WHERE session_id = :s"), {"s": session_id})
            await db.execute(text("DELETE FROM trade_sessions WHERE id = :s"), {"s": session_id})
            await db.execute(text("DELETE FROM strategies WHERE id = :s"), {"s": strategy_id})
            await db.commit()
    _run(go)


def test_paper_runner_blocks_within_cooldown():
    """Two signals 1 min apart with cooldown=5m → first OK, second REJECTED."""
    strategy_id, user_id = _make_strategy(cooldown_min=5, max_trades_per_day=99, max_open_positions=99)
    session_id = _make_session(user_id)
    try:
        # First entry: 1 min ago
        _record_entry(session_id, strategy_id, user_id, "NQ",
                      when=datetime.now(timezone.utc) - timedelta(minutes=1))
        # Second entry attempt now — should be rejected by cooldown
        async def attempt():
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="NQ", direction="long", mode="paper",
                open_positions_snapshot=[],  # no open positions
            )
        d = _run(attempt)
        assert isinstance(d, Decision)
        assert d.allowed is False, f"expected REJECTED, got {d.reason}"
        assert d.reason == "cooldown"
    finally:
        _cleanup(strategy_id, session_id)


def test_paper_runner_allows_after_cooldown():
    """Entry 10 min ago with cooldown=5m → new entry allowed."""
    strategy_id, user_id = _make_strategy(cooldown_min=5, max_trades_per_day=99, max_open_positions=99)
    session_id = _make_session(user_id)
    try:
        _record_entry(session_id, strategy_id, user_id, "NQ",
                      when=datetime.now(timezone.utc) - timedelta(minutes=10))
        async def attempt():
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="NQ", direction="long", mode="paper",
                open_positions_snapshot=[],
            )
        d = _run(attempt)
        assert d.allowed is True, f"expected ALLOWED after cooldown, got {d.reason}"
    finally:
        _cleanup(strategy_id, session_id)


def test_paper_runner_max_trades_per_day():
    """11 signals; cap=10 → exactly 10 allowed, 11th rejected."""
    strategy_id, user_id = _make_strategy(cooldown_min=0, max_trades_per_day=10, max_open_positions=99)
    session_id = _make_session(user_id)
    try:
        # Record 10 trades, each 10 min apart so cooldown is not the blocker
        base = datetime.now(timezone.utc) - timedelta(hours=2)
        for i in range(10):
            _record_entry(session_id, strategy_id, user_id, "NQ",
                          when=base + timedelta(minutes=i * 10))
        # Eleventh attempt — should be rejected
        async def attempt():
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="NQ", direction="long", mode="paper",
                open_positions_snapshot=[],
            )
        d = _run(attempt)
        assert d.allowed is False
        assert d.reason == "max_trades_per_day", f"got {d.reason}, debug={d.debug}"
    finally:
        _cleanup(strategy_id, session_id)


def test_paper_runner_max_open_positions():
    """Limit=1; snapshot has one open NQ; ES entry must still be REJECTED
    because total open exceeds limit (after the dup-instrument check passes
    for ES it falls through to max_open)."""
    strategy_id, user_id = _make_strategy(cooldown_min=0, max_trades_per_day=99, max_open_positions=1)
    session_id = _make_session(user_id)
    try:
        snapshot = [{"session_id": session_id, "instrument": "NQ"}]
        async def attempt_es():
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="ES", direction="long", mode="paper",
                open_positions_snapshot=snapshot,
            )
        d = _run(attempt_es)
        assert d.allowed is False
        assert d.reason == "max_open_positions", f"got {d.reason}, debug={d.debug}"
    finally:
        _cleanup(strategy_id, session_id)


def test_paper_runner_blocks_duplicate_instrument():
    """Limit=2 (so max_open isn't hit), snapshot has NQ open, another NQ
    must still be REJECTED by the dup-instrument check."""
    strategy_id, user_id = _make_strategy(cooldown_min=0, max_trades_per_day=99, max_open_positions=2)
    session_id = _make_session(user_id)
    try:
        snapshot = [{"session_id": session_id, "instrument": "NQ"}]
        async def attempt():
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="NQ", direction="long", mode="paper",
                open_positions_snapshot=snapshot,
            )
        d = _run(attempt)
        assert d.allowed is False
        assert d.reason == "duplicate_instrument"
    finally:
        _cleanup(strategy_id, session_id)


def test_paper_runner_clean_session_allowed():
    """Fresh session, no history → first entry must be allowed."""
    strategy_id, user_id = _make_strategy(cooldown_min=5, max_trades_per_day=10, max_open_positions=1)
    session_id = _make_session(user_id)
    try:
        async def attempt():
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="NQ", direction="long", mode="paper",
                open_positions_snapshot=[],
            )
        d = _run(attempt)
        assert d.allowed is True, f"expected ALLOWED, got reason={d.reason} debug={d.debug}"
    finally:
        _cleanup(strategy_id, session_id)
