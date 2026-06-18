"""Issue 1 + Issue 2 regression tests.

  * pnl_marks (pure): equities freeze at the official close outside RTH;
    futures roots classify precisely (ESTC must NOT read as futures).
  * entry_guard bar-clock cooldown: a stale replayed bar can no longer defeat
    the cooldown (the Judas-Swing 20x re-entry bug).
  * entry_guard same-price lockout: a just-closed setup can't immediately
    reopen at the same price even after the cooldown elapses.

The guard tests hit the live test DB, mirroring test_paper_runner_cooldown.py.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from sqlalchemy import text

from app.database import async_session_factory, engine
from app.engines.entry_guard import can_enter, ensure_strategy_columns, Decision
from app.engines import pnl_marks as pm


# ───────────────────────── pure pnl_marks tests ─────────────────────────
def test_is_futures_symbol_precise():
    assert pm.is_futures_symbol("NQ")
    assert pm.is_futures_symbol("ES")
    assert pm.is_futures_symbol("MNQ")
    assert pm.is_futures_symbol("NQZ5")    # month/year coded contract
    assert pm.is_futures_symbol("ESH26")
    # Equity tickers that merely start with a root must NOT classify as futures
    assert not pm.is_futures_symbol("ESTC")
    assert not pm.is_futures_symbol("ROKU")
    assert not pm.is_futures_symbol("YMAB")
    assert not pm.is_futures_symbol("")


def test_equity_mark_frozen_outside_rth():
    """The bug: after-hours lastTrade ticks moved displayed P&L. Outside RTH we
    must mark at the official close (day.c), never lastTrade."""
    tj = {"lastTrade": {"p": 118.0}, "day": {"c": 140.0}, "prevDay": {"c": 139.0}}
    # Regular session → live last trade
    px, src = pm.pick_equity_mark(tj, "regular")
    assert px == 118.0 and src.startswith("last_trade")
    # After hours → frozen at today's official close (NOT the 118 after-hours print)
    px, src = pm.pick_equity_mark(tj, "afterhours")
    assert px == 140.0 and src.startswith("day_close")
    # Pre-market with no settled day close → yesterday's close
    px, src = pm.pick_equity_mark({"lastTrade": {"p": 50.0}, "prevDay": {"c": 49.0}}, "premarket")
    assert px == 49.0 and src.startswith("prev_close")
    # Closed overnight → today's official close still wins
    px, src = pm.pick_equity_mark(tj, "closed")
    assert px == 140.0 and src.startswith("day_close")


def test_market_session_label():
    assert pm.market_session_label("NQ") in ("rth", "globex")
    assert pm.market_session_label("ROKU") in ("regular", "premarket", "afterhours", "closed")


# ───────────────────────── guard test scaffolding ───────────────────────
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


def _make_strategy(cooldown_min=5, max_trades_per_day=99, max_open_positions=99):
    async def go():
        await ensure_strategy_columns()
        sid = str(uuid.uuid4())
        async with async_session_factory() as db:
            uid = str((await db.execute(text("SELECT id FROM users LIMIT 1"))).fetchone()[0])
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
            """), {"id": sid, "uid": uid, "nm": f"test-barclock-{sid[:8]}",
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


def _record_entry(session_id, strategy_id, user_id, instrument, when, price=100.0, direction="long"):
    async def go():
        tid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO trades
                    (id, strategy_id, user_id, session_id, mode, status,
                     instrument, direction, contracts, entry_price, stop_loss,
                     take_profit, entry_time, pnl, commission, net_pnl)
                VALUES (:id, :sid, :uid, :sess, 'paper', 'closed',
                        :inst, :dir, 1, :px, 99.0,
                        102.0, :t, 0, 0, 0)
            """), {"id": tid, "sid": strategy_id, "uid": user_id, "sess": session_id,
                    "inst": instrument, "dir": direction, "px": price, "t": when})
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


def test_stale_bar_cannot_defeat_cooldown():
    """THE FIX. A prior entry happened at a STALE bar timestamp (20 min ago in
    wall-clock, simulating yfinance's ~11-min replay lag). The next candidate
    bar is only 1 minute later in bar-clock. Old code compared now() (far
    ahead) to the stale entry_time → elapsed >>cooldown → ALLOWED (the bug).
    New code compares bar-clock to bar-clock → 60s < 300s → REJECTED."""
    strategy_id, user_id = _make_strategy(cooldown_min=5)
    session_id = _make_session(user_id)
    try:
        prior = datetime.now(timezone.utc) - timedelta(minutes=20)   # stale prior entry
        candidate = prior + timedelta(minutes=1)                      # next bar, 1m later
        _record_entry(session_id, strategy_id, user_id, "NQ", when=prior, price=30665.0)
        async def attempt():
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="NQ", direction="long", mode="paper",
                open_positions_snapshot=[], bar_time=candidate, entry_price=30700.0,
            )
        d = _run(attempt)
        assert d.allowed is False, f"stale-bar re-entry should be blocked, got {d.reason}"
        assert d.reason == "cooldown", f"expected cooldown, got {d.reason}"
    finally:
        _cleanup(strategy_id, session_id)


def test_same_price_reentry_blocked_after_cooldown():
    """A setup that closed at 30665 must not reopen at ~30665 within the lockout
    window even after the cooldown elapses."""
    strategy_id, user_id = _make_strategy(cooldown_min=5)
    session_id = _make_session(user_id)
    try:
        prior = datetime.now(timezone.utc) - timedelta(minutes=10)   # past 5m cooldown
        _record_entry(session_id, strategy_id, user_id, "NQ", when=prior,
                      price=30665.25, direction="short")
        async def attempt(px):
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="NQ", direction="short", mode="paper",
                open_positions_snapshot=[],
                bar_time=datetime.now(timezone.utc), entry_price=px,
            )
        # Same price band → blocked even though cooldown elapsed
        d_same = _run(lambda: attempt(30665.25))
        assert d_same.allowed is False and d_same.reason == "same_price_reentry", \
            f"same-price re-entry should be blocked, got {d_same.reason}"
        # A clearly different level → allowed
        d_diff = _run(lambda: attempt(30720.00))
        assert d_diff.allowed is True, f"different level should be allowed, got {d_diff.reason}"
    finally:
        _cleanup(strategy_id, session_id)


def test_live_realtime_bars_unaffected():
    """Live engine passes real-time bars (bar_time≈now). A 10-min-old prior with
    a 5-min cooldown and a fresh, differently-priced candidate is allowed."""
    strategy_id, user_id = _make_strategy(cooldown_min=5)
    session_id = _make_session(user_id)
    try:
        prior = datetime.now(timezone.utc) - timedelta(minutes=10)
        _record_entry(session_id, strategy_id, user_id, "NQ", when=prior, price=30000.0)
        async def attempt():
            return await can_enter(
                session_id=session_id, strategy_id=strategy_id,
                instrument="NQ", direction="long", mode="live",
                open_positions_snapshot=[],
                bar_time=datetime.now(timezone.utc), entry_price=30500.0,
            )
        d = _run(attempt)
        assert d.allowed is True, f"expected ALLOWED, got {d.reason}"
    finally:
        _cleanup(strategy_id, session_id)
