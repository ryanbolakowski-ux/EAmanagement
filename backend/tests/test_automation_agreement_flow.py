"""Activate-Automation agreement flow (#160) — backend regression tests:

  * Signing the fully_automated_trading agreement is IDEMPOTENT: re-signing the
    same version returns the existing record, inserts no duplicate row, and does
    NOT re-fire the admin notification.
  * A NEW signing emails the admin/owner exactly once (notify_admins_security).
  * /legal/status reports accepted + accepted_at for the signed agreement so the
    UI can show "signed on <date>".

DB-backed, isolated-throwaway-row pattern (mirrors tests/test_auto_trade_guard.py).
The route functions are called directly with a stub Request + a real throwaway
tier_5 user; notify_admins_security is monkeypatched to capture calls.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text, select

from app.database import async_session_factory, engine
import app.api.routes.security as sec
from app.api.routes.legal import (
    record_acknowledgment, get_my_ack_status, AcknowledgmentCreate,
)
from app.models.user import User


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
    if "e" in out:
        raise out["e"]
    return out.get("v")


class _Req:
    """Minimal stand-in for fastapi Request (only what the route reads)."""
    class _C: host = "203.0.113.9"
    client = _C()
    headers = {"user-agent": "pytest-agent/1.0"}
    # fastapi Request.headers.get(...) — dict.get works for our usage
    def __init__(self):
        self.headers = {"user-agent": "pytest-agent/1.0"}


def _make_user(tier="tier_5"):
    async def go():
        uid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO users (id, email, username, hashed_password, is_active,
                                   is_admin, subscription_tier, created_at)
                VALUES (:id, :em, :un, 'x', true, false, :tier, now())
            """), {"id": uid, "em": f"auto-{uid[:8]}@test.local",
                    "un": f"auto-{uid[:8]}", "tier": tier})
            await db.commit()
        return uid
    return _run(go)


def _ack_count(uid, kind):
    async def go():
        async with async_session_factory() as db:
            return (await db.execute(text(
                "SELECT count(*) FROM user_acknowledgments WHERE user_id=:u AND kind=:k"
            ), {"u": uid, "k": kind})).scalar() or 0
    return _run(go)


def _cleanup(uid):
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("DELETE FROM user_acknowledgments WHERE user_id=:u"), {"u": uid})
            await db.execute(text("DELETE FROM security_audit_log WHERE user_id=:u"), {"u": uid})
            await db.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})
            await db.commit()
    _run(go)


def test_acknowledge_idempotent_and_notifies_admin_once():
    uid = _make_user("tier_5")
    captured = []
    orig = sec.notify_admins_security
    async def _capture(subject, html):
        captured.append((subject, html))
    sec.notify_admins_security = _capture
    try:
        async def sign():
            async with async_session_factory() as db:
                user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
                r1 = await record_acknowledgment(
                    AcknowledgmentCreate(kind="fully_automated_trading", detail="I agree"),
                    _Req(), user, db)
                r2 = await record_acknowledgment(
                    AcknowledgmentCreate(kind="fully_automated_trading", detail="I agree again"),
                    _Req(), user, db)
                return r1, r2
        r1, r2 = _run(sign)
        # Same version -> same row id returned, no duplicate inserted.
        assert r1.id == r2.id, "re-signing must return the same ack row, not a new one"
        assert _ack_count(uid, "fully_automated_trading") == 1, "no duplicate ack row"
        # Admin notified exactly once (on the first signing only).
        assert len(captured) == 1, f"admin should be notified once, got {len(captured)}"
        subj, html = captured[0]
        assert "signed" in subj.lower()
        assert "Fully Automated Trading Agreement" in html
        assert "tier_5" in html  # plan/package included
    finally:
        sec.notify_admins_security = orig
        _cleanup(uid)


def test_status_returns_accepted_at():
    uid = _make_user("tier_5")
    orig = sec.notify_admins_security
    async def _noop(subject, html): pass
    sec.notify_admins_security = _noop
    try:
        async def sign_then_status():
            async with async_session_factory() as db:
                user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
                await record_acknowledgment(
                    AcknowledgmentCreate(kind="fully_automated_trading", detail="ok"),
                    _Req(), user, db)
                return await get_my_ack_status(user, db)
        status = _run(sign_then_status)
        fat = status["acknowledgments"]["fully_automated_trading"]
        assert fat["accepted"] is True
        assert fat["accepted_at"], "accepted_at must be populated after signing"
        # parseable ISO timestamp
        datetime.fromisoformat(fat["accepted_at"])
        assert fat["current_version"]
    finally:
        sec.notify_admins_security = orig
        _cleanup(uid)


def test_unsigned_status_has_no_accepted_at():
    uid = _make_user("tier_5")
    try:
        async def go():
            async with async_session_factory() as db:
                user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
                return await get_my_ack_status(user, db)
        status = _run(go)
        fat = status["acknowledgments"]["fully_automated_trading"]
        assert fat["accepted"] is False
        assert fat["accepted_at"] is None
    finally:
        _cleanup(uid)
