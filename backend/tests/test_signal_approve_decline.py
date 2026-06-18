"""Phase-F approve/decline decision recording + can_place_on_approval routing.

Integration tests for app/api/routes/account_signals.py:
  * GET  /{id}/review   -> get_signal_for_review
  * POST /{id}/approve  -> approve_signal
  * POST /{id}/decline  -> decline_signal
  * helper _signal_to_tradesignal

We drive the underlying async route functions directly (a full HTTP client
with auth is not available in this isolated context). Each function takes a
``current_user`` (a transient User whose tier we control), a real AsyncSession,
and a Request — so we construct a tiny fake Request and a throwaway User.

Follows the isolated-throwaway-row pattern from test_paper_runner_cooldown.py:
a _run() thread+asyncio.run wrapper, engine.dispose() before/after, uuid
throwaway user/strategy/watcher/signal rows, and a _cleanup() that DELETEs
them. NEVER mutate real rows.
"""
import asyncio
import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.database import async_session_factory, engine
from app.models.user import User
from app.api.routes import account_signals as asig
from app.api.routes.account_signals import (
    get_signal_for_review, approve_signal, decline_signal, _signal_to_tradesignal,
)
from app.core.packages import requires_manual_approval, can_place_on_approval
from app.engines.account_signals import runner as asig_runner
from app.engines.strategy_engine.base_strategy import TradeSignal, SignalType
from fastapi import HTTPException


# ─── isolated-loop runner (same pattern as test_paper_runner_cooldown.py) ────

def _run(coro_factory):
    """Run an async factory on its own thread+loop (keeps the shared async
    engine off any throwaway loop). Exceptions raised inside the coroutine are
    captured and RE-RAISED on the calling thread so pytest.raises() works."""
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
        except BaseException as e:  # noqa: BLE001 - re-raised on the main thread
            out["err"] = e
    t = threading.Thread(target=worker); t.start(); t.join()
    if "err" in out:
        raise out["err"]
    return out["v"]


# ─── fake Request so the handlers can read client.host / user-agent ──────────

class _FakeClient:
    host = "127.0.0.1"

class _FakeRequest:
    client = _FakeClient()
    headers = {"user-agent": "pytest-approve-decline"}


def _user(uid, tier):
    """A transient (NOT persisted) User. The handlers only read .id and
    .subscription_tier from it; the SQL queries scope by .id which we point at
    our throwaway DB user, so FK-backed writes (decided_by is a plain column)
    succeed."""
    u = User()
    u.id = uuid.UUID(uid)
    u.subscription_tier = tier
    return u


# ─── throwaway-row provisioning ──────────────────────────────────────────────

def _make_fixtures(tier="tier_5"):
    """Insert a throwaway user(tier) + strategy + watcher, return their ids."""
    async def go():
        uid = str(uuid.uuid4())
        sid = str(uuid.uuid4())
        wid = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO users (id, email, username, hashed_password,
                                   is_active, subscription_tier)
                VALUES (CAST(:id AS uuid), :em, :un, '!disabled-test!', TRUE, :tier)
            """), {"id": uid, "em": f"approve-{uid[:8]}@thetaalgos.test",
                    "un": f"approve_{uid[:8]}", "tier": tier})
            await db.execute(text("""
                INSERT INTO strategies (id, user_id, name, status)
                VALUES (CAST(:id AS uuid), CAST(:uid AS uuid), :nm, 'active')
            """), {"id": sid, "uid": uid, "nm": f"approve-strat-{sid[:8]}"})
            await db.execute(text("""
                INSERT INTO account_signal_watchers
                    (id, user_id, strategy_id, account_label)
                VALUES (CAST(:id AS uuid), CAST(:uid AS uuid), CAST(:sid AS uuid), :lbl)
            """), {"id": wid, "uid": uid, "sid": sid, "lbl": f"acct-{wid[:8]}"})
            await db.commit()
        return uid, sid, wid
    return _run(go)


def _make_signal(uid, sid, wid, instrument="NQ", direction="long",
                 entry=20000.0, stop=19950.0, target=20100.0, status="sent"):
    """Insert one account_signals row (decision NULL by default)."""
    async def go():
        sig_id = str(uuid.uuid4())
        async with async_session_factory() as db:
            await db.execute(text("""
                INSERT INTO account_signals
                    (id, watcher_id, user_id, strategy_id, instrument, direction,
                     entry_price, stop_loss, take_profit, fired_at, status)
                VALUES (CAST(:id AS uuid), CAST(:wid AS uuid), CAST(:uid AS uuid),
                        CAST(:sid AS uuid), :inst, :dir, :e, :s, :t, NOW(), :st)
            """), {"id": sig_id, "wid": wid, "uid": uid, "sid": sid,
                    "inst": instrument, "dir": direction, "e": entry, "s": stop,
                    "t": target, "st": status})
            await db.commit()
        return sig_id
    return _run(go)


def _fetch_signal(sig_id):
    async def go():
        async with async_session_factory() as db:
            return (await db.execute(text("""
                SELECT decision, decided_at, decided_via, placed_ref, status
                  FROM account_signals WHERE id = CAST(:id AS uuid)
            """), {"id": sig_id})).first()
    return _run(go)


def _count_audit(uid, event_type):
    async def go():
        async with async_session_factory() as db:
            return (await db.execute(text("""
                SELECT COUNT(*) FROM security_audit_log
                 WHERE user_id = CAST(:uid AS uuid) AND event_type = :ev
            """), {"uid": uid, "ev": event_type})).scalar()
    return _run(go)


def _cleanup(uid, sid):
    async def go():
        async with async_session_factory() as db:
            await db.execute(text("DELETE FROM security_audit_log WHERE user_id = CAST(:uid AS uuid)"), {"uid": uid})
            await db.execute(text("DELETE FROM account_signals WHERE user_id = CAST(:uid AS uuid)"), {"uid": uid})
            await db.execute(text("DELETE FROM account_signal_watchers WHERE user_id = CAST(:uid AS uuid)"), {"uid": uid})
            await db.execute(text("DELETE FROM strategies WHERE id = CAST(:sid AS uuid)"), {"sid": sid})
            await db.execute(text("DELETE FROM users WHERE id = CAST(:uid AS uuid)"), {"uid": uid})
            await db.commit()
    _run(go)


# ─── helpers to invoke the handlers inside one DB session on the iso loop ─────

def _call_review(uid, signal_id, tier):
    async def go():
        async with async_session_factory() as db:
            return await get_signal_for_review(signal_id, current_user=_user(uid, tier), db=db)
    return _run(go)


def _call_decline(uid, signal_id, tier):
    async def go():
        async with async_session_factory() as db:
            return await decline_signal(signal_id, _FakeRequest(),
                                        current_user=_user(uid, tier), db=db)
    return _run(go)


def _call_approve(uid, signal_id, tier):
    async def go():
        async with async_session_factory() as db:
            return await approve_signal(signal_id, _FakeRequest(),
                                        current_user=_user(uid, tier), db=db)
    return _run(go)


# ═════════════════════════════════════════════════════════════════════════════
# TESTS
# ═════════════════════════════════════════════════════════════════════════════

def test_review_returns_row_plus_tier_caps():
    """GET /{id}/review for an owned signal returns the row plus
    requires_manual_approval / can_place_on_approval computed from the caller's
    tier. tier_4 -> requires_manual_approval True, can_place_on_approval True."""
    uid, sid, wid = _make_fixtures(tier="tier_4")
    sig_id = _make_signal(uid, sid, wid, instrument="ES", direction="short",
                          entry=5000.0, stop=5020.0, target=4960.0)
    try:
        out = _call_review(uid, sig_id, "tier_4")
        assert out["id"] == sig_id
        assert out["instrument"] == "ES"
        assert out["direction"] == "short"
        assert out["entry_price"] == 5000.0
        assert out["status"] == "sent"
        assert out["decision"] is None
        # caps reflect tier_4 (manual-approve tier that MAY place)
        assert out["requires_manual_approval"] is requires_manual_approval(_user(uid, "tier_4"))
        assert out["requires_manual_approval"] is True
        assert out["can_place_on_approval"] is can_place_on_approval(_user(uid, "tier_4"))
        assert out["can_place_on_approval"] is True
    finally:
        _cleanup(uid, sid)


def test_review_404_for_missing_or_other_user():
    """A non-existent signal id -> 404; another user's signal id -> 404."""
    uid, sid, wid = _make_fixtures(tier="tier_4")
    # second user owns a separate signal
    uid2, sid2, wid2 = _make_fixtures(tier="tier_4")
    other_sig = _make_signal(uid2, sid2, wid2)
    try:
        # totally unknown id
        with pytest.raises(HTTPException) as ei:
            _call_review(uid, str(uuid.uuid4()), "tier_4")
        assert ei.value.status_code == 404
        assert ei.value.detail == "Signal not found."
        # other user's signal, viewed as uid -> 404 (owner-scoped)
        with pytest.raises(HTTPException) as ei2:
            _call_review(uid, other_sig, "tier_4")
        assert ei2.value.status_code == 404
    finally:
        _cleanup(uid, sid)
        _cleanup(uid2, sid2)


def test_decline_records_and_places_nothing(monkeypatch):
    """POST /{id}/decline on a sent, undecided signal sets decision='declined',
    decided_at, decided_via='app', returns {placed:False}, writes
    EVENT_TRADE_DECLINED, and places NOTHING (route_emitted_signal untouched)."""
    uid, sid, wid = _make_fixtures(tier="tier_4")
    sig_id = _make_signal(uid, sid, wid)

    called = {"n": 0}
    async def _spy(*a, **k):
        called["n"] += 1
        return []
    monkeypatch.setattr(asig_runner, "route_emitted_signal", _spy)

    try:
        out = _call_decline(uid, sig_id, "tier_4")
        assert out["placed"] is False
        assert out["decision"] == "declined"

        row = _fetch_signal(sig_id)
        assert row.decision == "declined"
        assert row.decided_at is not None
        assert row.decided_via == "app"
        # nothing placed
        assert called["n"] == 0
        # audit row written
        from app.api.routes.security import EVENT_TRADE_DECLINED
        assert _count_audit(uid, EVENT_TRADE_DECLINED) == 1
    finally:
        _cleanup(uid, sid)


def test_approve_tier_not_eligible_records_only(monkeypatch):
    """POST /{id}/approve when can_place_on_approval is False (tier_2) ->
    decision='approved', placed_ref ==
    'approved_signal_only(tier_not_eligible_to_place)', route_emitted_signal
    NOT called, EVENT_TRADE_APPROVED audited."""
    uid, sid, wid = _make_fixtures(tier="tier_2")
    sig_id = _make_signal(uid, sid, wid)

    called = {"n": 0}
    async def _spy(*a, **k):
        called["n"] += 1
        return []
    monkeypatch.setattr(asig_runner, "route_emitted_signal", _spy)

    try:
        # sanity: tier_2 may not place
        assert can_place_on_approval(_user(uid, "tier_2")) is False
        out = _call_approve(uid, sig_id, "tier_2")
        assert out["decision"] == "approved"
        assert out["placed_ref"] == "approved_signal_only(tier_not_eligible_to_place)"

        row = _fetch_signal(sig_id)
        assert row.decision == "approved"
        assert row.placed_ref == "approved_signal_only(tier_not_eligible_to_place)"
        assert called["n"] == 0  # routing never attempted
        from app.api.routes.security import EVENT_TRADE_APPROVED
        assert _count_audit(uid, EVENT_TRADE_APPROVED) == 1
    finally:
        _cleanup(uid, sid)


def test_approve_eligible_routes_once_with_mapped_args(monkeypatch):
    """can_place_on_approval True (tier_4) -> route_emitted_signal called exactly
    once with (signal_id, user_id, instrument, TradeSignal, strategy_id); the
    TradeSignal carries the mapped direction/prices; placed_ref reflects the
    routed result."""
    uid, sid, wid = _make_fixtures(tier="tier_4")
    sig_id = _make_signal(uid, sid, wid, instrument="NQ", direction="long",
                          entry=20000.0, stop=19950.0, target=20100.0)

    captured = {"calls": []}
    async def _spy(signal_id, user_id, instrument, tradesignal, strategy_id):
        captured["calls"].append((signal_id, user_id, instrument, tradesignal, strategy_id))
        # one paper session entered -> approve handler maps to "paper:entered"
        return [("paper", "k1", True, "ok")]
    monkeypatch.setattr(asig_runner, "route_emitted_signal", _spy)

    try:
        out = _call_approve(uid, sig_id, "tier_4")
        assert out["decision"] == "approved"
        # placed_ref reflects the routed tuple ("paper", key, entered=True, reason)
        assert out["placed_ref"] == "paper:entered"

        assert len(captured["calls"]) == 1, "route_emitted_signal must be called exactly once"
        c_sigid, c_uid, c_inst, c_ts, c_strat = captured["calls"][0]
        assert c_sigid == sig_id
        assert c_uid == uid
        assert c_inst == "NQ"
        assert c_strat == sid
        # the TradeSignal carries mapped direction + entry/stop/target
        assert isinstance(c_ts, TradeSignal)
        assert c_ts.signal == SignalType.LONG
        assert c_ts.entry_price == 20000.0
        assert c_ts.stop_loss == 19950.0
        assert c_ts.take_profit == 20100.0

        row = _fetch_signal(sig_id)
        assert row.placed_ref == "paper:entered"
    finally:
        _cleanup(uid, sid)


def test_approve_eligible_no_session_marks_no_active(monkeypatch):
    """When route_emitted_signal returns empty (no active eligible session),
    placed_ref == 'approved_no_active_eligible_session'."""
    uid, sid, wid = _make_fixtures(tier="tier_5")
    sig_id = _make_signal(uid, sid, wid)

    async def _spy(*a, **k):
        return []
    monkeypatch.setattr(asig_runner, "route_emitted_signal", _spy)

    try:
        # tier_5 also can_place_on_approval
        assert can_place_on_approval(_user(uid, "tier_5")) is True
        out = _call_approve(uid, sig_id, "tier_5")
        assert out["placed_ref"] == "approved_no_active_eligible_session"
        row = _fetch_signal(sig_id)
        assert row.placed_ref == "approved_no_active_eligible_session"
        assert row.decision == "approved"
    finally:
        _cleanup(uid, sid)


def test_approve_route_error_is_recorded_not_500(monkeypatch):
    """If route_emitted_signal raises, placed_ref starts with
    'approved_place_error:' and the request still succeeds (decision recorded,
    no exception bubbles)."""
    uid, sid, wid = _make_fixtures(tier="tier_4")
    sig_id = _make_signal(uid, sid, wid)

    async def _boom(*a, **k):
        raise RuntimeError("broker down")
    monkeypatch.setattr(asig_runner, "route_emitted_signal", _boom)

    try:
        out = _call_approve(uid, sig_id, "tier_4")
        assert out["decision"] == "approved"
        assert out["placed_ref"].startswith("approved_place_error:")
        # the RuntimeError class name is recorded
        assert "RuntimeError" in out["placed_ref"]
        row = _fetch_signal(sig_id)
        assert row.decision == "approved"
        assert row.placed_ref.startswith("approved_place_error:")
    finally:
        _cleanup(uid, sid)


def test_double_decision_rejected_409(monkeypatch):
    """Decision is single-shot: approve then approve again -> 409 'Already
    approved.'; and approve then decline -> 409 'Already approved.'"""
    uid, sid, wid = _make_fixtures(tier="tier_2")  # tier_2: approve records only
    sig_id = _make_signal(uid, sid, wid)
    async def _spy(*a, **k):
        return []
    monkeypatch.setattr(asig_runner, "route_emitted_signal", _spy)

    try:
        first = _call_approve(uid, sig_id, "tier_2")
        assert first["decision"] == "approved"
        # second approve -> 409
        with pytest.raises(HTTPException) as ei:
            _call_approve(uid, sig_id, "tier_2")
        assert ei.value.status_code == 409
        assert ei.value.detail == "Already approved."
        # decline after approve -> still 409 'Already approved.'
        with pytest.raises(HTTPException) as ei2:
            _call_decline(uid, sig_id, "tier_2")
        assert ei2.value.status_code == 409
        assert ei2.value.detail == "Already approved."
    finally:
        _cleanup(uid, sid)


def test_decision_is_owner_and_status_scoped():
    """decline/approve on a signal whose status != 'sent' -> 404; and on a
    signal owned by another user -> 404 (owner-scoped via _record_decision)."""
    uid, sid, wid = _make_fixtures(tier="tier_4")
    # a suppressed (not 'sent') signal owned by uid
    suppressed_sig = _make_signal(uid, sid, wid, status="suppressed")
    # a 'sent' signal owned by a different user
    uid2, sid2, wid2 = _make_fixtures(tier="tier_4")
    other_sig = _make_signal(uid2, sid2, wid2, status="sent")
    try:
        # status-scoped: suppressed -> 404 on decline AND approve
        with pytest.raises(HTTPException) as e1:
            _call_decline(uid, suppressed_sig, "tier_4")
        assert e1.value.status_code == 404
        with pytest.raises(HTTPException) as e2:
            _call_approve(uid, suppressed_sig, "tier_4")
        assert e2.value.status_code == 404
        # owner-scoped: other user's sent signal -> 404 when uid acts on it
        with pytest.raises(HTTPException) as e3:
            _call_decline(uid, other_sig, "tier_4")
        assert e3.value.status_code == 404
    finally:
        _cleanup(uid, sid)
        _cleanup(uid2, sid2)


def test_signal_to_tradesignal_maps_long_and_short():
    """_signal_to_tradesignal maps direction 'long'->LONG and 'short'->SHORT
    with entry/stop/target carried through. We exercise the pure helper through
    a tiny row-like shim AND through the approve path (args route receives)."""
    class _Row:
        def __init__(self, direction):
            self.direction = direction
            self.instrument = "NQ"
            self.entry_price = 20000.0
            self.stop_loss = 19950.0
            self.take_profit = 20100.0

    ts_long = _signal_to_tradesignal(_Row("long"))
    assert ts_long.signal == SignalType.LONG
    assert ts_long.instrument == "NQ"
    assert ts_long.entry_price == 20000.0
    assert ts_long.stop_loss == 19950.0
    assert ts_long.take_profit == 20100.0

    ts_short = _signal_to_tradesignal(_Row("short"))
    assert ts_short.signal == SignalType.SHORT
    assert ts_short.entry_price == 20000.0
    assert ts_short.stop_loss == 19950.0
    assert ts_short.take_profit == 20100.0
