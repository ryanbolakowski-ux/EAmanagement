"""live_trading.py Phase-D activation gates.

Exercises the PHASE-D-GUARD logic in app/api/routes/live_trading.py directly:

  * PATCH /accounts/{id}/trading-enabled  (set_account_trading_enabled)
  * POST  /sessions                       (start_live_session)
  * PATCH /accounts/{id}/sizing           (update_account_sizing)

For a tier_5 (fully-automated) user, ENABLING automation on a NON-sandbox
account requires the live_trading_consent + risk_disclosure +
fully_automated_trading acks AND a recently-consumed 'enable_automation'
verification code. RAISING a risk knob requires the risk_change ack + a
recently-consumed 'risk_change' code. Lowering / first-time setup / disabling /
sandbox accounts bypass the gates.

DB-backed. Follows the isolated-loop throwaway-row pattern from
test_paper_runner_cooldown.py / test_legal_acknowledgments.py: every DB touch
runs inside _run() on its own thread+event-loop with engine.dispose()
bracketing; every row we create uses a uuid throwaway user / broker-account /
ack / verification-code / audit row, and _cleanup() DELETEs them. We NEVER
mutate real user rows.

We import the REAL feature functions and call the async route handlers directly
with a tiny stand-in user object (the handlers only read .id /
.subscription_tier / .email) and a real AsyncSession + minimal Request — no
full HTTP client is needed and none is depended on by the gate logic.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timezone, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.database import async_session_factory, engine
from app.api.routes.live_trading import (
    set_account_trading_enabled,
    start_live_session,
    update_account_sizing,
    TradingEnabledRequest,
    StartLiveSessionRequest,
    SizingUpdate,
)
from app.api.routes.legal import CURRENT_VERSIONS
from app.api.routes.security import (
    EVENT_AUTOMATION_ENABLED,
    EVENT_AUTOMATION_DISABLED,
    EVENT_RISK_CHANGE,
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

    t = threading.Thread(target=worker)
    t.start()
    t.join()
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
                   "email": f"phase-d-gate-{uid}@thetaalgos.test",
                   "uname": f"phase_d_gate_{uid[:8]}",
                   "tier": tier})
            await db.commit()
        return uid
    return _run(go)


def _make_account(user_id, *, sandbox_mode=False, trading_enabled=False,
                  risk_per_trade_usd=None, risk_per_trade_pct=None,
                  max_position_usd=None, account_type="cash"):
    """Create a throwaway broker account, return its id (str)."""
    async def go():
        aid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO broker_accounts
                    (id, user_id, broker, account_name, encrypted_credentials,
                     is_demo, sandbox_mode, is_active, trading_enabled,
                     account_type, risk_per_trade_usd, risk_per_trade_pct,
                     max_position_usd, created_at)
                VALUES (:id, :uid, 'tradier', :nm, 'enc-test',
                        FALSE, :sb, TRUE, :te,
                        :atype, :rusd, :rpct, :maxpos, NOW())
            """), {"id": aid, "uid": user_id, "nm": f"acct-{aid[:8]}",
                   "sb": sandbox_mode, "te": trading_enabled,
                   "atype": account_type, "rusd": risk_per_trade_usd,
                   "rpct": risk_per_trade_pct, "maxpos": max_position_usd})
            await db.commit()
        return aid
    return _run(go)


def _record_ack(user_id, kind, content_version=None):
    """Insert a throwaway ack row at the CURRENT version (unless overridden)."""
    ver = content_version or CURRENT_VERSIONS.get(kind, "v1")

    async def go():
        aid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO user_acknowledgments
                    (id, user_id, kind, content_version, agreed_at)
                VALUES (:id, :uid, :kind, :ver, NOW())
            """), {"id": aid, "uid": user_id, "kind": kind, "ver": ver})
            await db.commit()
        return aid
    return _run(go)


def _record_consumed_code(user_id, purpose, *, consumed_minutes_ago=1):
    """Insert a verification_codes row that's already CONSUMED within the recent
    window, so require_recent_verification(purpose) passes."""
    async def go():
        cid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        consumed = now - timedelta(minutes=consumed_minutes_ago)
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO verification_codes
                    (id, user_id, purpose, code_hash, context, created_at,
                     expires_at, attempts, consumed_at)
                VALUES (:id, :uid, :p, 'deadbeef', CAST('{}' AS JSONB), :created,
                        :exp, 0, :consumed)
            """), {"id": cid, "uid": user_id, "p": purpose,
                   "created": consumed - timedelta(seconds=5),
                   "exp": now + timedelta(minutes=9), "consumed": consumed})
            await db.commit()
        return cid
    return _run(go)


def _audit_count(user_id, event_type):
    """How many security_audit_log rows of event_type exist for this user."""
    async def go():
        async with async_session_factory() as db:
            r = await db.execute(text("""
                SELECT count(*) AS n FROM security_audit_log
                 WHERE user_id = :uid AND event_type = :ev
            """), {"uid": user_id, "ev": event_type})
            return int(r.fetchone().n)
    return _run(go)


def _account_trading_enabled(account_id):
    async def go():
        async with async_session_factory() as db:
            r = await db.execute(text(
                "SELECT trading_enabled FROM broker_accounts WHERE id = :a"
            ), {"a": account_id})
            row = r.fetchone()
            return None if row is None else bool(row.trading_enabled)
    return _run(go)


def _account_risk_usd(account_id):
    """Persisted risk_per_trade_usd (the response model doesn't echo it)."""
    async def go():
        async with async_session_factory() as db:
            r = await db.execute(text(
                "SELECT risk_per_trade_usd FROM broker_accounts WHERE id = :a"
            ), {"a": account_id})
            row = r.fetchone()
            return None if row is None else row.risk_per_trade_usd
    return _run(go)


def _cleanup(*user_ids):
    async def go():
        async with async_session_factory() as db:
            for uid in user_ids:
                await db.execute(text("DELETE FROM security_audit_log WHERE user_id = :u"), {"u": uid})
                await db.execute(text("DELETE FROM verification_codes WHERE user_id = :u"), {"u": uid})
                await db.execute(text("DELETE FROM user_acknowledgments WHERE user_id = :u"), {"u": uid})
                await db.execute(text("DELETE FROM trades WHERE user_id = :u"), {"u": uid})
                await db.execute(text("DELETE FROM broker_accounts WHERE user_id = :u"), {"u": uid})
                await db.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            await db.commit()
    _run(go)


# A stand-in user: the gate logic reads .id, .subscription_tier, .email only.
class _U:
    def __init__(self, uid, tier="tier_5", email="phase-d@thetaalgos.test"):
        self.id = uid
        self.subscription_tier = tier
        self.email = email


class _Req:
    """Minimal stand-in for starlette Request — audit_log reads
    request.client.host and request.headers.get('user-agent')."""
    class _Client:
        host = "127.0.0.1"
    client = _Client()
    headers = {"user-agent": "pytest"}


def _all_enable_acks(uid):
    _record_ack(uid, "live_trading_consent")
    _record_ack(uid, "risk_disclosure")
    _record_ack(uid, "fully_automated_trading")


# ===========================================================================
# PATCH /trading-enabled  (set_account_trading_enabled)
# ===========================================================================
def test_trading_enabled_missing_first_ack_403():
    """tier_5, non-sandbox, NO acks -> 403 on the FIRST missing ack
    (live_trading_consent)."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, trading_enabled=False)
    try:
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await set_account_trading_enabled(
                    account_id=aid,
                    data=TradingEnabledRequest(trading_enabled=True),
                    request=_Req(), current_user=user, db=db,
                )
        with pytest.raises(HTTPException) as ei:
            _run(call)
        assert ei.value.status_code == 403
        ver = CURRENT_VERSIONS["live_trading_consent"]
        assert ei.value.detail == f"acknowledgment_required:live_trading_consent:{ver}"
        # No flip, nothing audited as enabled.
        assert _account_trading_enabled(aid) is False
        assert _audit_count(uid, EVENT_AUTOMATION_ENABLED) == 0
    finally:
        _cleanup(uid)


def test_trading_enabled_acks_present_but_no_code_403_verification():
    """All three acks present but NO recent enable_automation code consumed ->
    403 verification_required:enable_automation."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, trading_enabled=False)
    try:
        _all_enable_acks(uid)
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await set_account_trading_enabled(
                    account_id=aid,
                    data=TradingEnabledRequest(trading_enabled=True),
                    request=_Req(), current_user=user, db=db,
                )
        with pytest.raises(HTTPException) as ei:
            _run(call)
        assert ei.value.status_code == 403
        assert ei.value.detail == "verification_required:enable_automation"
        assert _account_trading_enabled(aid) is False
        assert _audit_count(uid, EVENT_AUTOMATION_ENABLED) == 0
    finally:
        _cleanup(uid)


def test_trading_enabled_with_acks_and_code_succeeds_and_audits():
    """All three acks AND a recent consumed enable_automation code -> 200,
    trading_enabled flips True, EVENT_AUTOMATION_ENABLED audited."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, trading_enabled=False)
    try:
        _all_enable_acks(uid)
        _record_consumed_code(uid, "enable_automation", consumed_minutes_ago=1)
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await set_account_trading_enabled(
                    account_id=aid,
                    data=TradingEnabledRequest(trading_enabled=True),
                    request=_Req(), current_user=user, db=db,
                )
        resp = _run(call)
        assert resp.trading_enabled is True
        assert _account_trading_enabled(aid) is True
        assert _audit_count(uid, EVENT_AUTOMATION_ENABLED) == 1
    finally:
        _cleanup(uid)


def test_trading_disabled_never_requires_consent_and_audits_disabled():
    """Turning trading_enabled OFF always succeeds (safety release) with NO
    acks/verification, and audits EVENT_AUTOMATION_DISABLED."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, trading_enabled=True)
    try:
        # No acks, no codes — disable must still work.
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await set_account_trading_enabled(
                    account_id=aid,
                    data=TradingEnabledRequest(trading_enabled=False),
                    request=_Req(), current_user=user, db=db,
                )
        resp = _run(call)
        assert resp.trading_enabled is False
        assert _account_trading_enabled(aid) is False
        assert _audit_count(uid, EVENT_AUTOMATION_DISABLED) == 1
        assert _audit_count(uid, EVENT_AUTOMATION_ENABLED) == 0
    finally:
        _cleanup(uid)


def test_trading_enabled_on_sandbox_bypasses_all_gates():
    """Enabling on a SANDBOX account bypasses the consent/agreement/verification
    gates entirely (the `and not account.sandbox_mode` guard). No acks, no code,
    still 200 and flips True. Sandbox enable is NOT audited as automation."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=True, trading_enabled=False)
    try:
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await set_account_trading_enabled(
                    account_id=aid,
                    data=TradingEnabledRequest(trading_enabled=True),
                    request=_Req(), current_user=user, db=db,
                )
        resp = _run(call)
        assert resp.trading_enabled is True
        assert _account_trading_enabled(aid) is True
        # sandbox-enable path skips the EVENT_AUTOMATION_ENABLED audit.
        assert _audit_count(uid, EVENT_AUTOMATION_ENABLED) == 0
    finally:
        _cleanup(uid)


# ===========================================================================
# POST /sessions  (start_live_session) — mirrors the same gate
# ===========================================================================
def test_start_live_session_missing_fully_automated_ack_403():
    """tier_5 session: has live_trading_consent + risk_disclosure but NOT
    fully_automated_trading -> 403 acknowledgment_required:fully_automated_trading:v1."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, trading_enabled=False)
    try:
        _record_ack(uid, "live_trading_consent")
        _record_ack(uid, "risk_disclosure")
        # deliberately NOT fully_automated_trading
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await start_live_session(
                    data=StartLiveSessionRequest(
                        strategy_id=str(uuid.uuid4()),
                        broker_account_id=aid,
                    ),
                    request=_Req(), current_user=user, db=db,
                )
        with pytest.raises(HTTPException) as ei:
            _run(call)
        assert ei.value.status_code == 403
        ver = CURRENT_VERSIONS["fully_automated_trading"]
        assert ei.value.detail == f"acknowledgment_required:fully_automated_trading:{ver}"
    finally:
        _cleanup(uid)


def test_start_live_session_missing_code_403_verification():
    """tier_5 session: all three acks but NO recent enable_automation code ->
    403 verification_required:enable_automation (gate fires before strategy lookup)."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, trading_enabled=False)
    try:
        _all_enable_acks(uid)
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await start_live_session(
                    data=StartLiveSessionRequest(
                        strategy_id=str(uuid.uuid4()),
                        broker_account_id=aid,
                    ),
                    request=_Req(), current_user=user, db=db,
                )
        with pytest.raises(HTTPException) as ei:
            _run(call)
        assert ei.value.status_code == 403
        assert ei.value.detail == "verification_required:enable_automation"
    finally:
        _cleanup(uid)


# ===========================================================================
# PATCH /sizing  (update_account_sizing) — risk-knob raise gate
# ===========================================================================
def test_risk_raise_missing_ack_403():
    """Raising risk_per_trade_usd (new > old) without the risk_change ack ->
    403 acknowledgment_required:risk_change:v1."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, risk_per_trade_usd=100.0,
                        risk_per_trade_pct=None)
    try:
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await update_account_sizing(
                    account_id=aid,
                    data=SizingUpdate(account_type="cash",
                                      risk_per_trade_usd=250.0,
                                      risk_per_trade_pct=None),
                    request=_Req(), current_user=user, db=db,
                )
        with pytest.raises(HTTPException) as ei:
            _run(call)
        assert ei.value.status_code == 403
        ver = CURRENT_VERSIONS["risk_change"]
        assert ei.value.detail == f"acknowledgment_required:risk_change:{ver}"
        assert _audit_count(uid, EVENT_RISK_CHANGE) == 0
    finally:
        _cleanup(uid)


def test_risk_raise_ack_present_but_no_code_403_verification():
    """Raising risk with the risk_change ack present but NO recent risk_change
    code -> 403 verification_required:risk_change."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, risk_per_trade_usd=100.0,
                        risk_per_trade_pct=None)
    try:
        _record_ack(uid, "risk_change")
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await update_account_sizing(
                    account_id=aid,
                    data=SizingUpdate(account_type="cash",
                                      risk_per_trade_usd=250.0,
                                      risk_per_trade_pct=None),
                    request=_Req(), current_user=user, db=db,
                )
        with pytest.raises(HTTPException) as ei:
            _run(call)
        assert ei.value.status_code == 403
        assert ei.value.detail == "verification_required:risk_change"
        assert _audit_count(uid, EVENT_RISK_CHANGE) == 0
    finally:
        _cleanup(uid)


def test_risk_lower_is_allowed_no_ack_no_audit():
    """LOWERING a risk knob (new < old) is always allowed with no ack/verification
    and does NOT audit EVENT_RISK_CHANGE."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, risk_per_trade_usd=500.0,
                        risk_per_trade_pct=None)
    try:
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await update_account_sizing(
                    account_id=aid,
                    data=SizingUpdate(account_type="cash",
                                      risk_per_trade_usd=200.0,
                                      risk_per_trade_pct=None),
                    request=_Req(), current_user=user, db=db,
                )
        resp = _run(call)
        # The response model doesn't echo risk knobs; verify the persisted value
        # and that the handler returned a BrokerAccountResponse (no raise).
        assert resp.trading_enabled is not None
        assert _account_risk_usd(aid) == 200.0
        assert _audit_count(uid, EVENT_RISK_CHANGE) == 0
    finally:
        _cleanup(uid)


def test_risk_first_time_setup_is_allowed_no_ack_no_audit():
    """First-time setup (old is None) is always allowed with no ack/verification
    and does NOT audit EVENT_RISK_CHANGE (the _raised() check requires old !=
    None)."""
    uid = _make_user("tier_5")
    aid = _make_account(uid, sandbox_mode=False, risk_per_trade_usd=None,
                        risk_per_trade_pct=None)
    try:
        user = _U(uid)

        async def call():
            async with async_session_factory() as db:
                return await update_account_sizing(
                    account_id=aid,
                    data=SizingUpdate(account_type="cash",
                                      risk_per_trade_usd=300.0,
                                      risk_per_trade_pct=None),
                    request=_Req(), current_user=user, db=db,
                )
        resp = _run(call)
        assert resp.trading_enabled is not None
        assert _account_risk_usd(aid) == 300.0
        assert _audit_count(uid, EVENT_RISK_CHANGE) == 0
    finally:
        _cleanup(uid)
