"""auto_trade_guard.auto_trade_allowed — fail-closed matrix.

Phase-E execution-time backstop: no UNATTENDED auto live trade may be placed
unless the user is tier_5 (fully-automated package), has accepted the Fully
Automated Trading Agreement, AND the broker account has trading_enabled. Every
BLOCK is audited (EVENT_AUTO_TRADE_BLOCKED). Fail-CLOSED on any internal error.

DB-backed integration style: we create throwaway user / broker_account /
acknowledgment rows, call the REAL auto_trade_allowed (which opens its own
session via async_session_factory), and assert on the (allowed, reason) tuple
plus the security_audit_log row it writes. Every throwaway row is cleaned up.
We NEVER touch real user rows.

Follows the isolated-loop pattern from test_paper_runner_cooldown.py:
the _run() thread+asyncio.run wrapper with engine.dispose() before/after.
"""
import asyncio
import threading
import uuid

import pytest
from sqlalchemy import text

from app.database import async_session_factory, engine
from app.api.routes.security import EVENT_AUTO_TRADE_BLOCKED
# Import the REAL feature function under test (do not reimplement).
from app.core.auto_trade_guard import auto_trade_allowed


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


# ---------------------------------------------------------------------------
# Throwaway-row helpers (ISOLATED — uuid rows, deleted in finally).
# ---------------------------------------------------------------------------
def _make_user(tier="tier_5"):
    """Create a throwaway user with the given subscription_tier. Returns id."""
    async def go():
        uid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO users (id, email, username, hashed_password,
                                   is_active, subscription_tier)
                VALUES (:id, :email, :uname, '!login-disabled-test-fixture!',
                        TRUE, :tier)
            """), {"id": uid,
                    "email": f"atg-{uid}@thetaalgos.test",
                    "uname": f"atg_{uid[:12]}",
                    "tier": tier})
            await db.commit()
        return uid
    return _run(go)


def _grant_ack(user_id, kind="fully_automated_trading", ver="v1"):
    """Insert the current-version acknowledgment row for the user."""
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO user_acknowledgments
                    (id, user_id, kind, content_version, agreed_at)
                VALUES (:id, :uid, :kind, :ver, NOW())
            """), {"id": str(uuid.uuid4()), "uid": user_id,
                    "kind": kind, "ver": ver})
            await db.commit()
    _run(go)


def _make_account(user_id, trading_enabled=True):
    """Create a throwaway broker_account for the user. Returns id."""
    async def go():
        aid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO broker_accounts
                    (id, user_id, broker, account_name, encrypted_credentials,
                     trading_enabled)
                VALUES (:id, :uid, 'tradovate', :nm, 'x', :te)
            """), {"id": aid, "uid": user_id,
                    "nm": f"atg-acct-{aid[:8]}", "te": trading_enabled})
            await db.commit()
        return aid
    return _run(go)


def _latest_block_detail(user_id):
    """Return the JSON detail of the most-recent EVENT_AUTO_TRADE_BLOCKED row
    for this user (or None if none was written)."""
    async def go():
        async with async_session_factory() as db:
            r = (await db.execute(text("""
                SELECT detail FROM security_audit_log
                 WHERE user_id = :uid AND event_type = :ev
                 ORDER BY created_at DESC
                 LIMIT 1
            """), {"uid": user_id, "ev": EVENT_AUTO_TRADE_BLOCKED})).fetchone()
        return r[0] if r else None
    return _run(go)


def _cleanup(user_id, account_id=None):
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("DELETE FROM security_audit_log WHERE user_id = :u"), {"u": user_id})
            await db.execute(text("DELETE FROM user_acknowledgments WHERE user_id = :u"), {"u": user_id})
            if account_id:
                await db.execute(text("DELETE FROM broker_accounts WHERE id = :a"), {"a": account_id})
            await db.execute(text("DELETE FROM broker_accounts WHERE user_id = :u"), {"u": user_id})
            await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
            await db.commit()
    _run(go)


def _call(user_id, broker_account_id):
    """Invoke the real async guard inside the isolated loop."""
    return _run(lambda: auto_trade_allowed(user_id, broker_account_id,
                                           context={"src": "pytest"}))


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------
def test_user_id_none_blocks_and_audits():
    """user_id=None -> (False, 'user_not_found'); BLOCK audited (uid NULL row)."""
    allowed, reason = _call(None, None)
    assert allowed is False
    assert reason == "user_not_found"
    # The audit row is written with user_id NULL; verify by event+reason.
    async def go():
        async with async_session_factory() as db:
            r = (await db.execute(text("""
                SELECT detail FROM security_audit_log
                 WHERE user_id IS NULL AND event_type = :ev
                   AND detail->>'reason' = 'user_not_found'
                   AND detail->>'src' = 'pytest'
                 ORDER BY created_at DESC LIMIT 1
            """), {"ev": EVENT_AUTO_TRADE_BLOCKED})).fetchone()
            # best-effort cleanup of the NULL-user rows we just created
            await db.execute(text("""
                DELETE FROM security_audit_log
                 WHERE user_id IS NULL AND event_type = :ev
                   AND detail->>'src' = 'pytest'
            """), {"ev": EVENT_AUTO_TRADE_BLOCKED})
            await db.commit()
        return r[0] if r else None  # detail JSONB -> dict
    detail = _run(go)
    assert detail is not None, "expected an EVENT_AUTO_TRADE_BLOCKED row with user_not_found"
    assert detail.get("reason") == "user_not_found"


def test_non_tier5_blocked_with_exact_tier_in_reason():
    """tier_4 user -> (False, 'not_fully_automated_package(tier=tier_4)')."""
    uid = _make_user(tier="tier_4")
    try:
        allowed, reason = _call(uid, None)
        assert allowed is False
        assert reason == "not_fully_automated_package(tier=tier_4)"
        detail = _latest_block_detail(uid)
        assert detail is not None
        assert detail.get("reason") == "not_fully_automated_package(tier=tier_4)"
    finally:
        _cleanup(uid)


def test_tier5_missing_agreement_blocked():
    """tier_5 but no fully_automated_trading ack -> blocked."""
    uid = _make_user(tier="tier_5")
    try:
        allowed, reason = _call(uid, None)
        assert allowed is False
        assert reason == "fully_automated_trading_agreement_not_accepted"
        detail = _latest_block_detail(uid)
        assert detail is not None
        assert detail.get("reason") == "fully_automated_trading_agreement_not_accepted"
    finally:
        _cleanup(uid)


def test_tier5_agreed_but_account_trading_disabled_blocked():
    """tier_5 + ack + account.trading_enabled=False -> blocked."""
    uid = _make_user(tier="tier_5")
    aid = None
    try:
        _grant_ack(uid)
        aid = _make_account(uid, trading_enabled=False)
        allowed, reason = _call(uid, aid)
        assert allowed is False
        assert reason == "account_trading_enabled_off"
        detail = _latest_block_detail(uid)
        assert detail is not None
        assert detail.get("reason") == "account_trading_enabled_off"
    finally:
        _cleanup(uid, aid)


def test_tier5_agreed_no_account_blocked():
    """tier_5 + ack + broker_account_id=None -> BLOCKED (fail-closed: an
    unattended live trade must target a real, trading-enabled account)."""
    uid = _make_user(tier="tier_5")
    try:
        _grant_ack(uid)
        allowed, reason = _call(uid, None)
        assert allowed is False
        assert reason == "no_broker_account"
        detail = _latest_block_detail(uid)
        assert detail is not None
        assert detail.get("reason") == "no_broker_account"
    finally:
        _cleanup(uid)


def test_tier5_agreed_account_enabled_allowed():
    """tier_5 + ack + account.trading_enabled=True -> ok."""
    uid = _make_user(tier="tier_5")
    aid = None
    try:
        _grant_ack(uid)
        aid = _make_account(uid, trading_enabled=True)
        allowed, reason = _call(uid, aid)
        assert allowed is True
        assert reason == "ok"
        assert _latest_block_detail(uid) is None
    finally:
        _cleanup(uid, aid)


def test_internal_exception_fails_closed(monkeypatch):
    """Any internal error -> fail-closed (False, 'guard_error:<ExceptionType>'),
    trade NOT allowed. We monkeypatch the lazily-imported async_session_factory
    on app.database so the guard's `from app.database import ...` resolves to a
    raising factory."""
    import app.database as dbmod

    class _Boom(RuntimeError):
        pass

    def _raise(*a, **k):
        raise _Boom("kaboom")

    monkeypatch.setattr(dbmod, "async_session_factory", _raise, raising=True)

    allowed, reason = _call("00000000-0000-0000-0000-000000000000", None)
    assert allowed is False, "must fail CLOSED on internal error"
    assert reason == "guard_error:_Boom", f"got {reason!r}"


def test_all_blocked_paths_return_false_first_element():
    """Cross-check: every blocked construction returns allowed=False as the
    first tuple element. (ok paths covered separately.)"""
    # None user
    assert _call(None, None)[0] is False
    # tier_4
    uid4 = _make_user(tier="tier_4")
    try:
        assert _call(uid4, None)[0] is False
    finally:
        _cleanup(uid4)
    # tier_5 no ack
    uid5 = _make_user(tier="tier_5")
    try:
        assert _call(uid5, None)[0] is False
    finally:
        _cleanup(uid5)
    # clean up any NULL-user rows the first _call wrote
    async def _clean_null():
        async with async_session_factory() as db:
            await db.execute(text("""
                DELETE FROM security_audit_log
                 WHERE user_id IS NULL AND event_type = :ev
                   AND detail->>'src' = 'pytest'
            """), {"ev": EVENT_AUTO_TRADE_BLOCKED})
            await db.commit()
    _run(_clean_null)
