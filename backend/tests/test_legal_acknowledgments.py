"""Legal acknowledgments — version-specific has_current_ack / require_current_ack
behavior, the CURRENT_VERSIONS surface, and the acknowledge/status/documents
HTTP handlers.

DB-backed. Follows the isolated-loop throwaway-row pattern from
test_paper_runner_cooldown.py: every DB touch runs inside _run() on its own
thread+event-loop with engine.dispose() bracketing, all rows we create use uuid
throwaway users/acks, and _cleanup() DELETEs them. We NEVER mutate real user
rows — each test mints its own throwaway user(s) and deletes them + their acks.

We import the REAL feature functions from app.api.routes.legal and exercise
them directly. For the HTTP handlers (record_acknowledgment, get_my_ack_status,
get_disclosure_document) we call the async route function directly with a tiny
stand-in user object (only .id / .subscription_tier are read) and a real
AsyncSession — no full HTTP client needed.
"""
import asyncio
import threading
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.database import async_session_factory, engine
from app.api.routes.legal import (
    CURRENT_VERSIONS,
    DOCUMENTS,
    has_current_ack,
    require_current_ack,
    get_document,
    get_disclosure_document,
    record_acknowledgment,
    get_my_ack_status,
    AcknowledgmentCreate,
)


# ---------------------------------------------------------------------------
# isolated-loop runner (same shape as test_paper_runner_cooldown.py)
# ---------------------------------------------------------------------------
def _run(coro_factory):
    out = {}
    def worker():
        async def wrap():
            await engine.dispose()
            try:
                return await coro_factory()
            finally:
                await engine.dispose()
        try:
            out["v"] = asyncio.run(wrap())
        except BaseException as e:  # capture so we can re-raise in caller thread
            out["exc"] = e
    t = threading.Thread(target=worker); t.start(); t.join()
    if "exc" in out:
        raise out["exc"]
    return out.get("v")


# ---------------------------------------------------------------------------
# throwaway-row helpers
# ---------------------------------------------------------------------------
def _make_user(tier="tier_5"):
    """Create a throwaway user row, return its id (str)."""
    async def go():
        uid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO users (id, email, username, hashed_password,
                                   is_active, subscription_tier)
                VALUES (:id, :email, :uname, '!login-disabled-test!',
                        TRUE, :tier)
            """), {"id": uid,
                   "email": f"legal-test-{uid}@thetaalgos.test",
                   "uname": f"legal_test_{uid[:8]}",
                   "tier": tier})
            await db.commit()
        return uid
    return _run(go)


def _record_ack(user_id, kind, content_version):
    """Insert a throwaway acknowledgment row with an explicit content_version
    (used to plant stale-version rows the route handler would never write)."""
    async def go():
        aid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO user_acknowledgments
                    (id, user_id, kind, content_version, agreed_at)
                VALUES (:id, :uid, :kind, :ver, NOW())
            """), {"id": aid, "uid": user_id, "kind": kind, "ver": content_version})
            await db.commit()
        return aid
    return _run(go)


def _cleanup(*user_ids):
    async def go():
        async with async_session_factory() as db:
            for uid in user_ids:
                await db.execute(text("DELETE FROM user_acknowledgments WHERE user_id = :u"), {"u": uid})
                await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await db.commit()
    _run(go)


# A stand-in user: the route handlers only read .id and .subscription_tier.
class _U:
    def __init__(self, uid, tier="tier_5"):
        self.id = uid
        self.subscription_tier = tier


class _Req:
    """Minimal stand-in for starlette Request — record_acknowledgment reads
    request.client.host and request.headers.get('user-agent')."""
    class _Client:
        host = "127.0.0.1"
    client = _Client()
    headers = {"user-agent": "pytest"}


# ===========================================================================
# CURRENT_VERSIONS surface (regression for the package feature)
# ===========================================================================
def test_current_versions_surface_includes_new_feature_kinds():
    # The package/approval feature added these two kinds.
    assert CURRENT_VERSIONS.get("fully_automated_trading") == "v1"
    # signals_disclosure is present and versioned (real code is at v2).
    assert "signals_disclosure" in CURRENT_VERSIONS
    assert CURRENT_VERSIONS["signals_disclosure"] is not None
    # The pre-existing kinds are all still present.
    for k in ("terms_of_service", "risk_disclosure", "live_trading_consent",
              "options_trading_consent", "risk_change"):
        assert k in CURRENT_VERSIONS, f"missing kind {k}"
        assert CURRENT_VERSIONS[k], f"empty version for {k}"


# ===========================================================================
# has_current_ack — basic True/False
# ===========================================================================
def test_has_current_ack_false_before_then_true_after():
    uid = _make_user()
    try:
        ver = CURRENT_VERSIONS["fully_automated_trading"]
        # Before any ack -> False
        before = _run(lambda: _has(uid, "fully_automated_trading"))
        assert before is False
        # Record the CURRENT-version ack -> True
        _record_ack(uid, "fully_automated_trading", ver)
        after = _run(lambda: _has(uid, "fully_automated_trading"))
        assert after is True
    finally:
        _cleanup(uid)


async def _has(uid, kind):
    async with async_session_factory() as db:
        return await has_current_ack(db, uid, kind)


# ===========================================================================
# has_current_ack — version-specific (stale version doesn't count)
# ===========================================================================
def test_has_current_ack_is_version_specific():
    uid = _make_user()
    try:
        # Plant a STALE-version ack (v0) for the kind.
        _record_ack(uid, "fully_automated_trading", "v0")
        # Only the CURRENT version counts -> still False.
        res = _run(lambda: _has(uid, "fully_automated_trading"))
        assert res is False, "stale content_version must not satisfy has_current_ack"
    finally:
        _cleanup(uid)


# ===========================================================================
# has_current_ack — unknown kind returns False (no raise)
# ===========================================================================
def test_has_current_ack_unknown_kind_returns_false():
    uid = _make_user()
    try:
        res = _run(lambda: _has(uid, "nonexistent_kind_xyz"))
        assert res is False
    finally:
        _cleanup(uid)


# ===========================================================================
# has_current_ack — user-scoped (A's ack doesn't satisfy B)
# ===========================================================================
def test_has_current_ack_is_user_scoped():
    uid_a = _make_user()
    uid_b = _make_user()
    try:
        ver = CURRENT_VERSIONS["fully_automated_trading"]
        _record_ack(uid_a, "fully_automated_trading", ver)
        a = _run(lambda: _has(uid_a, "fully_automated_trading"))
        b = _run(lambda: _has(uid_b, "fully_automated_trading"))
        assert a is True
        assert b is False, "user B must not inherit user A's acknowledgment"
    finally:
        _cleanup(uid_a, uid_b)


# ===========================================================================
# require_current_ack — raises 403 w/ exact detail when missing, silent when present
# ===========================================================================
def test_require_current_ack_raises_then_passes():
    uid = _make_user()
    try:
        ver = CURRENT_VERSIONS["fully_automated_trading"]

        async def call_require():
            async with async_session_factory() as db:
                await require_current_ack(db, uid, "fully_automated_trading")

        # Missing -> HTTP 403 with exact detail
        with pytest.raises(HTTPException) as ei:
            _run(call_require)
        assert ei.value.status_code == 403
        assert ei.value.detail == f"acknowledgment_required:fully_automated_trading:{ver}"

        # Now record current ack -> must NOT raise
        _record_ack(uid, "fully_automated_trading", ver)
        # If it raises, _run would propagate; assert it returns cleanly (None).
        out = _run(lambda: _require_ok(uid, "fully_automated_trading"))
        assert out is None
    finally:
        _cleanup(uid)


async def _require_ok(uid, kind):
    async with async_session_factory() as db:
        await require_current_ack(db, uid, kind)
    return None


# ===========================================================================
# POST /legal/acknowledge persists server-resolved version (client can't spoof)
# + GET /legal/status reports accepted=True afterward
# ===========================================================================
def test_acknowledge_persists_server_version_and_status_reports_accepted():
    uid = _make_user()
    try:
        kind = "fully_automated_trading"
        server_ver = CURRENT_VERSIONS[kind]
        user = _U(uid)

        async def do_ack():
            async with async_session_factory() as db:
                # Note: AcknowledgmentCreate has no `version` field — the client
                # literally cannot send a version; the handler resolves it from
                # CURRENT_VERSIONS server-side.
                data = AcknowledgmentCreate(kind=kind, detail="i agree")
                resp = await record_acknowledgment(
                    data=data, request=_Req(), current_user=user, db=db,
                )
                return resp
        resp = _run(do_ack)
        assert resp.kind == kind
        # Server resolved the version, not the client.
        assert resp.content_version == server_ver

        # Verify what actually landed in the DB carries the server version.
        async def read_back():
            async with async_session_factory() as db:
                r = await db.execute(text("""
                    SELECT content_version FROM user_acknowledgments
                     WHERE user_id = :u AND kind = :k
                """), {"u": uid, "k": kind})
                return [row[0] for row in r.fetchall()]
        versions = _run(read_back)
        assert versions == [server_ver]

        # GET /legal/status reports accepted=True for that kind.
        async def do_status():
            async with async_session_factory() as db:
                return await get_my_ack_status(current_user=user, db=db)
        status_payload = _run(do_status)
        acks = status_payload["acknowledgments"]
        assert acks[kind]["accepted"] is True
        assert acks[kind]["current_version"] == server_ver
    finally:
        _cleanup(uid)


# ===========================================================================
# GET /legal/documents/{kind} — known kind returns title/version/html, unknown -> 404
# ===========================================================================
def test_get_disclosure_document_known_and_unknown():
    # Known: fully_automated_trading
    doc = _run(lambda: get_disclosure_document("fully_automated_trading"))
    assert doc["title"] == "Fully Automated Trading Agreement"
    assert doc["version"] == "v1"
    assert isinstance(doc["html"], str) and doc["html"].strip(), "html must be non-empty"

    # Unknown -> HTTPException 404
    with pytest.raises(HTTPException) as ei:
        _run(lambda: get_disclosure_document("not_a_real_doc"))
    assert ei.value.status_code == 404

    # Sanity: the pure get_document helper agrees.
    assert get_document("not_a_real_doc") is None
    assert get_document("fully_automated_trading")["version"] == "v1"
