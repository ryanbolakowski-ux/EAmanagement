"""Phase-F `GET /my-access` response-shape tests (app/api/routes/account_signals.py).

The endpoint handler `my_access(current_user, db)` is a plain async function whose
only dependencies are the signed-in `User` ORM object and an `AsyncSession`. Rather
than stand up the full HTTP client (the conftest fixture user is fixed at tier_5 and
shared across the session, so we cannot safely flip its tier), we call the handler
directly with a THROWAWAY user row whose tier we mutate, plus throwaway
acknowledgment / broker_account rows. Everything is created with uuid ids and torn
down in a finally, so no real user row is ever touched.

Isolated-loop pattern (engine.dispose() + thread + asyncio.run) mirrors
tests/test_paper_runner_cooldown.py so the shared async engine is never bound to a
throwaway loop.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text, select

from app.database import async_session_factory, engine
from app.models.user import User, BrokerAccount
from app.api.routes.account_signals import my_access


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
    if "exc" in out:
        raise out["exc"]
    return out.get("v")


# --- throwaway row helpers -------------------------------------------------

def _make_user(tier: str = "tier_5") -> str:
    """Insert a throwaway user at the given tier; return its id (str)."""
    async def go():
        uid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO users
                    (id, email, username, hashed_password, is_active, subscription_tier)
                VALUES
                    (:id, :email, :username, '!login-disabled-test!', TRUE, :tier)
            """), {"id": uid,
                    "email": f"my-access-{uid}@thetaalgos.test",
                    "username": f"my_access_{uid[:12]}",
                    "tier": tier})
            await db.commit()
        return uid
    return _run(go)


def _set_tier(uid: str, tier: str):
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("UPDATE users SET subscription_tier=:t WHERE id=:id"),
                             {"t": tier, "id": uid})
            await db.commit()
    _run(go)


def _add_ack(uid: str, kind: str, version: str):
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO user_acknowledgments
                    (id, user_id, kind, content_version, agreed_at)
                VALUES (:id, :uid, :kind, :ver, :now)
            """), {"id": str(uuid.uuid4()), "uid": uid, "kind": kind,
                    "ver": version, "now": datetime.now(timezone.utc)})
            await db.commit()
    _run(go)


def _add_broker_account(uid: str, trading_enabled: bool) -> str:
    async def go():
        aid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO broker_accounts
                    (id, user_id, broker, account_name, encrypted_credentials,
                     trading_enabled)
                VALUES (:id, :uid, 'tradovate', :nm, 'x', :te)
            """), {"id": aid, "uid": uid, "nm": f"acct-{aid[:8]}",
                    "te": trading_enabled})
            await db.commit()
        return aid
    return _run(go)


def _call_my_access(uid: str) -> dict:
    """Load the throwaway user as an ORM object inside a fresh session and invoke
    the real handler against the same session."""
    async def go():
        async with async_session_factory() as db:
            user = (await db.execute(
                select(User).where(User.id == uuid.UUID(uid))
            )).scalars().first()
            assert user is not None, "throwaway user not found"
            return await my_access(current_user=user, db=db)
    return _run(go)


def _cleanup(uid: str):
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("DELETE FROM broker_accounts WHERE user_id=:u"), {"u": uid})
            await db.execute(text("DELETE FROM user_acknowledgments WHERE user_id=:u"), {"u": uid})
            await db.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})
            await db.commit()
    _run(go)


REQUIRED_KEYS = {
    "tier", "fully_automated", "gets_signals", "requires_manual_approval",
    "can_place_on_approval", "automation_status", "agreements", "has_broker_account",
}


def _assert_shape(resp: dict):
    assert REQUIRED_KEYS.issubset(resp.keys()), f"missing keys: {REQUIRED_KEYS - set(resp)}"
    ag = resp["agreements"]
    assert isinstance(ag, dict)
    assert "fully_automated_trading" in ag
    assert "signals_disclosure_v2" in ag
    assert isinstance(ag["fully_automated_trading"], bool)
    assert isinstance(ag["signals_disclosure_v2"], bool)


# --- tests -----------------------------------------------------------------

def test_tier5_no_ack_agreement_required():
    uid = _make_user("tier_5")
    try:
        resp = _call_my_access(uid)
        _assert_shape(resp)
        assert resp["tier"] == "tier_5"
        assert resp["fully_automated"] is True
        assert resp["gets_signals"] is True
        assert resp["requires_manual_approval"] is False
        assert resp["can_place_on_approval"] is True
        assert resp["automation_status"] == "agreement_required"
        assert resp["agreements"]["fully_automated_trading"] is False
        assert resp["has_broker_account"] is False
    finally:
        _cleanup(uid)


def test_tier5_with_ack_no_account_disabled():
    uid = _make_user("tier_5")
    try:
        _add_ack(uid, "fully_automated_trading", "v1")
        resp = _call_my_access(uid)
        _assert_shape(resp)
        assert resp["agreements"]["fully_automated_trading"] is True
        # has_agreement True but no broker account => trading_enabled falsy => disabled
        assert resp["automation_status"] == "disabled"
        assert resp["has_broker_account"] is False
    finally:
        _cleanup(uid)


def test_tier5_with_ack_and_enabled_account_enabled():
    uid = _make_user("tier_5")
    try:
        _add_ack(uid, "fully_automated_trading", "v1")
        _add_broker_account(uid, trading_enabled=True)
        resp = _call_my_access(uid)
        _assert_shape(resp)
        assert resp["agreements"]["fully_automated_trading"] is True
        assert resp["has_broker_account"] is True
        assert resp["automation_status"] == "enabled"
    finally:
        _cleanup(uid)


def test_tier4_not_eligible_can_place():
    uid = _make_user("tier_4")
    try:
        resp = _call_my_access(uid)
        _assert_shape(resp)
        assert resp["tier"] == "tier_4"
        assert resp["fully_automated"] is False
        assert resp["requires_manual_approval"] is True
        assert resp["can_place_on_approval"] is True
        assert resp["gets_signals"] is True
        assert resp["automation_status"] == "not_eligible"
    finally:
        _cleanup(uid)


def test_tier2_signals_no_place():
    uid = _make_user("tier_2")
    try:
        resp = _call_my_access(uid)
        _assert_shape(resp)
        assert resp["tier"] == "tier_2"
        assert resp["can_place_on_approval"] is False
        assert resp["gets_signals"] is True
        assert resp["requires_manual_approval"] is True
        assert resp["automation_status"] == "not_eligible"
    finally:
        _cleanup(uid)


def test_free_trial_no_signals():
    uid = _make_user("free_trial")
    try:
        resp = _call_my_access(uid)
        _assert_shape(resp)
        assert resp["tier"] == "free_trial"
        assert resp["gets_signals"] is False
        assert resp["can_place_on_approval"] is False
        assert resp["automation_status"] == "not_eligible"
    finally:
        _cleanup(uid)


def test_agreements_fat_reflects_has_current_ack():
    """fully_automated_trading flips True only AFTER the v1 ack is recorded;
    a stale/wrong-version ack does not count."""
    uid = _make_user("tier_5")
    try:
        before = _call_my_access(uid)
        assert before["agreements"]["fully_automated_trading"] is False
        # wrong version should NOT satisfy has_current_ack (v1 is current)
        _add_ack(uid, "fully_automated_trading", "v0")
        mid = _call_my_access(uid)
        assert mid["agreements"]["fully_automated_trading"] is False
        # now record the current version
        _add_ack(uid, "fully_automated_trading", "v1")
        after = _call_my_access(uid)
        assert after["agreements"]["fully_automated_trading"] is True
    finally:
        _cleanup(uid)


def test_signals_disclosure_v2_reflects_has_current_ack():
    """agreements.signals_disclosure_v2 keys off has_current_ack('signals_disclosure')
    whose current version is v2."""
    uid = _make_user("tier_2")
    try:
        before = _call_my_access(uid)
        assert before["agreements"]["signals_disclosure_v2"] is False
        _add_ack(uid, "signals_disclosure", "v2")
        after = _call_my_access(uid)
        assert after["agreements"]["signals_disclosure_v2"] is True
    finally:
        _cleanup(uid)
