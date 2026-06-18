"""Email-code verification lifecycle — security.py.

Covers request_verification_code -> confirm_verification_code ->
require_recent_verification, plus the pure helpers (_gen_code/_hash_code) and
the /verify-code/request route's purpose validation.

DB-backed: hits the live test DB. Follows the isolated-throwaway-row pattern
from tests/test_paper_runner_cooldown.py — a _run() thread+asyncio.run wrapper,
engine.dispose() before/after, a uuid throwaway `users` row (verification_codes
and security_audit_log both FK to users), and a _cleanup() that DELETEs every
row we create. NEVER mutates real user rows.

The real send_verification_code_email is monkeypatched per-test so (a) no email
is sent and (b) we can capture the plaintext code the server generated (it is
otherwise stored only as a sha256 hash).
"""
import asyncio
import hashlib
import threading
import uuid
import types
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.database import async_session_factory, engine
from app.api.routes import security as sec
from app.api.routes.security import (
    _gen_code,
    _hash_code,
    request_verification_code,
    confirm_verification_code,
    require_recent_verification,
    post_request_code,
    CodeRequest,
    MAX_ATTEMPTS,
    RESEND_COOLDOWN_SEC,
    EVENT_CODE_SENT,
    EVENT_CODE_CONFIRMED,
    EVENT_CODE_FAILED,
)


# ── isolated-loop runner (same shape as test_paper_runner_cooldown.py) ────────
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


def _run_catch(coro_factory):
    """Like _run but returns the raised exception instead of propagating, so we
    can assert on HTTPException across the thread boundary."""
    out = {}
    def worker():
        async def wrap():
            await engine.dispose()
            try:
                return ("ok", await coro_factory())
            except Exception as e:  # noqa: BLE001
                return ("err", e)
            finally:
                await engine.dispose()
        out["v"] = asyncio.run(wrap())
    t = threading.Thread(target=worker); t.start(); t.join()
    return out["v"]


# ── throwaway user (FK target for verification_codes / security_audit_log) ────
def _make_user():
    async def go():
        uid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO users (id, email, username, hashed_password, is_active, subscription_tier)
                VALUES (:id, :email, :uname, '!login-disabled-test!', TRUE, 'tier_5')
            """), {"id": uid, "email": f"vcode-{uid[:8]}@thetaalgos.test",
                   "uname": f"vcode_{uid[:8]}"})
            await db.commit()
        return uid
    return _run(go)


def _user_obj(uid):
    """Minimal stand-in for the User model — the feature functions only read
    .id / .email / .username, so a SimpleNamespace is sufficient and avoids
    binding the ORM to the throwaway loop."""
    return types.SimpleNamespace(
        id=uid, email=f"vcode-{uid[:8]}@thetaalgos.test", username=f"vcode_{uid[:8]}")


def _cleanup(uid):
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("DELETE FROM verification_codes WHERE user_id = :u"), {"u": uid})
            await db.execute(text("DELETE FROM security_audit_log WHERE user_id = :u"), {"u": uid})
            await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await db.commit()
    _run(go)


def _capture_email_code():
    """Monkeypatch send_verification_code_email on the security module to swallow
    the send and capture the plaintext code. Returns a dict whose ['code'] is
    populated after request_verification_code runs."""
    box = {"code": None, "calls": 0}
    orig = sec.email_svc.send_verification_code_email

    def fake(*, to, username, code, purpose_label, ttl_min):  # noqa: ANN001
        box["code"] = code
        box["calls"] += 1
        return True
    sec.email_svc.send_verification_code_email = fake
    box["_restore"] = lambda: setattr(sec.email_svc, "send_verification_code_email", orig)
    return box


# DB helpers used by several tests ────────────────────────────────────────────
def _row_for(uid, purpose):
    async def go():
        async with async_session_factory() as db:
            r = await db.execute(text("""
                SELECT id, attempts, consumed_at, expires_at FROM verification_codes
                 WHERE user_id = :u AND purpose = :p ORDER BY created_at DESC LIMIT 1
            """), {"u": uid, "p": purpose})
            row = r.fetchone()
            return None if row is None else {
                "id": str(row.id), "attempts": row.attempts,
                "consumed_at": row.consumed_at, "expires_at": row.expires_at}
    return _run(go)


def _audit_events(uid):
    async def go():
        async with async_session_factory() as db:
            r = await db.execute(text("""
                SELECT event_type, detail FROM security_audit_log
                 WHERE user_id = :u ORDER BY created_at ASC
            """), {"u": uid})
            return [(x.event_type, x.detail) for x in r.fetchall()]
    return _run(go)


def _request(uid, purpose):
    """Run request_verification_code and return the captured plaintext code."""
    box = _capture_email_code()
    try:
        async def go():
            async with async_session_factory() as db:
                return await request_verification_code(db, _user_obj(uid), purpose, None, None)
        _run(go)
        return box["code"]
    finally:
        box["_restore"]()


def _confirm(uid, purpose, code):
    """Run confirm_verification_code, returning ('ok', bool) or ('err', exc)."""
    async def go():
        async with async_session_factory() as db:
            return await confirm_verification_code(db, _user_obj(uid), purpose, code, None)
    return _run_catch(go)


# ── helpers ───────────────────────────────────────────────────────────────────
def test_gen_code_is_6_digit_zero_padded_in_range():
    for _ in range(2000):
        c = _gen_code()
        assert isinstance(c, str)
        assert len(c) == 6 and c.isdigit()
        assert 100000 <= int(c) <= 999999


def test_hash_code_is_sha256_hex_and_stable():
    h = _hash_code("123456")
    assert h == hashlib.sha256(b"123456").hexdigest()
    assert len(h) == 64 and all(ch in "0123456789abcdef" for ch in h)
    assert _hash_code("123456") == h          # stable
    assert _hash_code("654321") != h          # different input → different hash


# ── happy path ────────────────────────────────────────────────────────────────
def test_happy_path_request_then_confirm():
    uid = _make_user()
    try:
        code = _request(uid, "enable_automation")
        assert code is not None and len(code) == 6
        before = _row_for(uid, "enable_automation")
        assert before is not None and before["consumed_at"] is None

        status, val = _confirm(uid, "enable_automation", code)
        assert status == "ok", f"expected confirm to succeed, got {val!r}"
        assert val is True

        after = _row_for(uid, "enable_automation")
        assert after["consumed_at"] is not None, "consumed_at should be set"

        evs = [e for (e, _d) in _audit_events(uid)]
        assert EVENT_CODE_SENT in evs
        assert EVENT_CODE_CONFIRMED in evs
    finally:
        _cleanup(uid)


# ── wrong code increments attempts + audits mismatch ─────────────────────────
def test_wrong_code_increments_attempts_and_audits_mismatch():
    uid = _make_user()
    try:
        code = _request(uid, "enable_automation")
        bad = "000000" if code != "000000" else "111111"

        for expected_attempts in (1, 2, 3):
            status, exc = _confirm(uid, "enable_automation", bad)
            assert status == "err" and isinstance(exc, HTTPException)
            assert exc.status_code == 400
            assert exc.detail == "Incorrect code."
            row = _row_for(uid, "enable_automation")
            assert row["attempts"] == expected_attempts, \
                f"attempts should be {expected_attempts}, got {row['attempts']}"
            assert row["consumed_at"] is None, "wrong code must not consume"

        details = [d for (e, d) in _audit_events(uid) if e == EVENT_CODE_FAILED]
        assert details, "EVENT_CODE_FAILED should be audited"
        assert all(d.get("reason") == "mismatch" for d in details), details
    finally:
        _cleanup(uid)


# ── lockout after MAX_ATTEMPTS ────────────────────────────────────────────────
def test_lockout_after_max_attempts():
    uid = _make_user()
    try:
        code = _request(uid, "enable_automation")
        bad = "000000" if code != "000000" else "111111"

        # MAX_ATTEMPTS failed confirms → attempts reaches MAX_ATTEMPTS, each 400.
        for _ in range(MAX_ATTEMPTS):
            status, exc = _confirm(uid, "enable_automation", bad)
            assert status == "err" and exc.status_code == 400

        row = _row_for(uid, "enable_automation")
        assert row["attempts"] == MAX_ATTEMPTS
        assert row["consumed_at"] is None, "locked code must not be consumed"

        # A further attempt is rejected as locked (429), code still not consumed.
        status, exc = _confirm(uid, "enable_automation", bad)
        assert status == "err" and isinstance(exc, HTTPException)
        assert exc.status_code == 429
        assert "Too many attempts" in exc.detail
        row = _row_for(uid, "enable_automation")
        assert row["consumed_at"] is None

        # Even the CORRECT code can't unlock it.
        status, exc = _confirm(uid, "enable_automation", code)
        assert status == "err" and exc.status_code == 429

        locked = [d for (e, d) in _audit_events(uid)
                  if e == EVENT_CODE_FAILED and d.get("reason") == "locked"]
        assert locked, "EVENT_CODE_FAILED reason='locked' should be audited"
    finally:
        _cleanup(uid)


# ── no active code (none requested) ───────────────────────────────────────────
def test_confirm_with_no_code_raises_no_active_code():
    uid = _make_user()
    try:
        status, exc = _confirm(uid, "enable_automation", "123456")
        assert status == "err" and isinstance(exc, HTTPException)
        assert exc.status_code == 400
        assert exc.detail.startswith("No active code")

        reasons = [d.get("reason") for (e, d) in _audit_events(uid) if e == EVENT_CODE_FAILED]
        assert "no_active_code" in reasons
    finally:
        _cleanup(uid)


# ── expired code is not selectable → "No active code" ─────────────────────────
def test_expired_code_is_not_selectable():
    uid = _make_user()
    try:
        # Insert a code row that already expired (expires_at in the past).
        plaintext = "424242"
        async def seed():
            async with async_session_factory() as db:
                now = datetime.now(timezone.utc)
                await db.execute(text("""
                    INSERT INTO verification_codes
                        (id, user_id, purpose, code_hash, context, created_at, expires_at, attempts)
                    VALUES (:id, :uid, 'enable_automation', :h, '{}'::jsonb, :created, :exp, 0)
                """), {"id": str(uuid.uuid4()), "uid": uid, "h": _hash_code(plaintext),
                       "created": now - timedelta(minutes=20),
                       "exp": now - timedelta(minutes=5)})
                await db.commit()
        _run(seed)

        # Even with the correct plaintext, an expired code is not selected.
        status, exc = _confirm(uid, "enable_automation", plaintext)
        assert status == "err" and isinstance(exc, HTTPException)
        assert exc.status_code == 400
        assert exc.detail.startswith("No active code")
    finally:
        _cleanup(uid)


# ── resend cooldown is per-purpose ────────────────────────────────────────────
def test_resend_cooldown_is_per_purpose():
    uid = _make_user()
    try:
        # First request for enable_automation succeeds.
        code1 = _request(uid, "enable_automation")
        assert code1 is not None

        # A second request for the SAME purpose within the window → 429.
        box = _capture_email_code()
        try:
            async def again():
                async with async_session_factory() as db:
                    return await request_verification_code(
                        db, _user_obj(uid), "enable_automation", None, None)
            status, exc = _run_catch(again)
        finally:
            box["_restore"]()
        assert status == "err" and isinstance(exc, HTTPException)
        assert exc.status_code == 429
        assert "Please wait" in exc.detail and exc.detail.endswith("s before requesting another code.")

        # A request for a DIFFERENT purpose is NOT blocked.
        code2 = _request(uid, "risk_change")
        assert code2 is not None and len(code2) == 6
    finally:
        _cleanup(uid)


# ── require_recent_verification: passes after confirm, scoped + windowed ───────
def test_require_recent_verification_passes_after_confirm():
    uid = _make_user()
    try:
        code = _request(uid, "enable_automation")
        status, _ = _confirm(uid, "enable_automation", code)
        assert status == "ok"

        # Within the window → no raise.
        async def ok():
            async with async_session_factory() as db:
                return await require_recent_verification(db, uid, "enable_automation")
        st, val = _run_catch(ok)
        assert st == "ok", f"expected pass, got {val!r}"

        # within_minutes=0 → cutoff is now; a consume slightly in the past is
        # outside the zero-width window → 403.
        async def zero():
            async with async_session_factory() as db:
                return await require_recent_verification(db, uid, "enable_automation", within_minutes=0)
        st, exc = _run_catch(zero)
        assert st == "err" and isinstance(exc, HTTPException)
        assert exc.status_code == 403
        assert exc.detail == "verification_required:enable_automation"
    finally:
        _cleanup(uid)


def test_require_recent_verification_is_purpose_scoped():
    uid = _make_user()
    try:
        code = _request(uid, "enable_automation")
        status, _ = _confirm(uid, "enable_automation", code)
        assert status == "ok"

        # Consuming an enable_automation code does NOT satisfy risk_change.
        async def other():
            async with async_session_factory() as db:
                return await require_recent_verification(db, uid, "risk_change")
        st, exc = _run_catch(other)
        assert st == "err" and isinstance(exc, HTTPException)
        assert exc.status_code == 403
        assert exc.detail == "verification_required:risk_change"
    finally:
        _cleanup(uid)


# ── route: unknown purpose rejected before any DB work ────────────────────────
def test_route_rejects_unknown_purpose():
    uid = _make_user()
    try:
        async def call():
            async with async_session_factory() as db:
                return await post_request_code(
                    CodeRequest(purpose="not_a_real_purpose", context=None),
                    request=None, current_user=_user_obj(uid), db=db)
        st, exc = _run_catch(call)
        assert st == "err" and isinstance(exc, HTTPException)
        assert exc.status_code == 400
        assert exc.detail == "Unknown verification purpose."
    finally:
        _cleanup(uid)


# ── single-use: a confirmed code cannot be replayed ───────────────────────────
def test_confirmed_code_cannot_be_replayed():
    uid = _make_user()
    try:
        code = _request(uid, "enable_automation")
        st1, v1 = _confirm(uid, "enable_automation", code)
        assert st1 == "ok" and v1 is True

        # Second confirm of the same (now consumed) code → No active code.
        st2, exc = _confirm(uid, "enable_automation", code)
        assert st2 == "err" and isinstance(exc, HTTPException)
        assert exc.status_code == 400
        assert exc.detail.startswith("No active code")
    finally:
        _cleanup(uid)
