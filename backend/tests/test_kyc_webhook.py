"""Unit tests for the Stripe Identity webhook handler in app.api.routes.kyc.

These tests do NOT hit the live backend on localhost:8000 — they import the
handler function directly and pass it a fake Request + fake AsyncSession.
That way they verify the v8-SDK StripeObject bug stays fixed even if the prod
container is offline.

Run standalone with:
    pytest backend/tests/test_kyc_webhook.py -v -p no:cacheprovider
"""
import json
import os
import sys
import types
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# --- Fake AsyncSession that records executed SQL + params -------------------
class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []
    def mappings(self): return self
    def all(self): return self._rows


class FakeAsyncSession:
    def __init__(self, rows=None):
        self.calls = []  # list of (sql, params)
        self.commits = 0
        self.rollbacks = 0
        self._rows = rows or []
    async def execute(self, stmt, params=None):
        try:
            sql_text = str(getattr(stmt, "text", stmt))
        except Exception:
            sql_text = str(stmt)
        self.calls.append((sql_text, params or {}))
        return _FakeResult(self._rows)
    async def commit(self): self.commits += 1
    async def rollback(self): self.rollbacks += 1
    async def refresh(self, obj): return None


# --- Fake Request ------------------------------------------------------------
class FakeRequest:
    def __init__(self, body=b"", headers=None):
        self._body = body
        self.headers = headers or {}
    async def body(self): return self._body


# --- StripeObject simulator ---------------------------------------------------
class FakeStripeObject:
    """Simulates the actual production bug. Acts like the real StripeObject:
    .get(...) raises AttributeError via __getattr__ (mirroring stripe SDK v8)."""
    def __init__(self, data):
        self.__dict__["_data"] = dict(data)
    def __getattr__(self, name):
        if name in self._data:
            v = self._data[name]
            if isinstance(v, dict):
                return FakeStripeObject(v)
            return v
        # CRITICAL: this is what crashes prod. .get on a StripeObject is not exposed.
        raise AttributeError(name)
    def __setattr__(self, name, value):
        self._data[name] = value
    def to_dict_recursive(self):
        # Real StripeObject returns plain dicts recursively.
        def _r(v):
            if isinstance(v, FakeStripeObject): return v.to_dict_recursive()
            if isinstance(v, dict): return {k: _r(x) for k, x in v.items()}
            if isinstance(v, list): return [_r(x) for x in v]
            return v
        return {k: _r(v) for k, v in self._data.items()}


def _mk_event(event_type, obj_dict, *, as_stripe_object=True):
    """Build the event object stripe.Webhook.construct_event() returns."""
    inner = FakeStripeObject(obj_dict) if as_stripe_object else obj_dict
    if as_stripe_object:
        evt = FakeStripeObject({"type": event_type, "data": {"object": inner._data if isinstance(inner, FakeStripeObject) else inner}})
        # event.data.object must be the StripeObject, not a plain dict
        evt._data["data"] = FakeStripeObject({"object": inner})
        return evt
    else:
        return {"type": event_type, "data": {"object": inner}}


# --- Test setup --------------------------------------------------------------
@pytest.fixture(autouse=True)
def _stripe_env(monkeypatch):
    """Make the handler think Stripe is configured."""
    monkeypatch.setenv("STRIPE_IDENTITY_WEBHOOK_SECRET", "whsec_test_dummy")
    monkeypatch.setenv("STRIPE_IDENTITY_KEY", "sk_test_dummy")
    # Reload module-level STRIPE_IDENTITY_KEY (set at import time)
    from app.api.routes import kyc as kyc_mod
    monkeypatch.setattr(kyc_mod, "STRIPE_IDENTITY_KEY", "sk_test_dummy", raising=True)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.new_event_loop().run_until_complete(coro)


def _call_webhook(event_or_factory, *, body=b"{}", sig="t=1,v1=x"):
    """Invoke kyc_webhook with construct_event patched to return the provided event."""
    from app.api.routes import kyc as kyc_mod
    req = FakeRequest(body=body, headers={"stripe-signature": sig})
    db = FakeAsyncSession()
    fake_stripe = MagicMock()
    fake_stripe.error = types.SimpleNamespace(SignatureVerificationError=Exception)
    if callable(event_or_factory) and not hasattr(event_or_factory, "_data"):
        fake_stripe.Webhook.construct_event.side_effect = event_or_factory
    else:
        fake_stripe.Webhook.construct_event.return_value = event_or_factory
    with patch.dict(sys.modules, {"stripe": fake_stripe}):
        result = asyncio.new_event_loop().run_until_complete(kyc_mod.kyc_webhook(req, db))
    return result, db


# --- Tests ------------------------------------------------------------------
def test_webhook_verified_event_updates_user():
    """type=verified, country=US -> user transitions to verified + audit row written."""
    evt = _mk_event("identity.verification_session.verified", {
        "id": "vs_TEST1",
        "metadata": {"user_id": "u-1"},
        "verified_outputs": {"address": {"country": "US"}},
    })
    result, db = _call_webhook(evt)
    assert result["status"] == "ok"
    assert result["event_type"] == "identity.verification_session.verified"
    assert result["session_id"] == "vs_TEST1"
    # Must have issued an UPDATE setting kyc_status='verified'
    updates = [c for c in db.calls if "kyc_status='verified'" in c[0]]
    assert updates, f"expected verified UPDATE; got: {[c[0] for c in db.calls]}"
    assert db.commits >= 1


def test_webhook_verified_non_us_marked_failed():
    """verified event with doc_country=CA -> user is marked failed, audit notes country."""
    evt = _mk_event("identity.verification_session.verified", {
        "id": "vs_TEST2",
        "metadata": {"user_id": "u-2"},
        "verified_outputs": {"address": {"country": "CA"}},
    })
    result, db = _call_webhook(evt)
    assert result["status"] == "ok"
    failed_updates = [c for c in db.calls if "kyc_status='failed'" in c[0]]
    assert failed_updates, f"expected failed UPDATE; got: {[c[0] for c in db.calls]}"
    # Audit insert should mention the country
    audit_inserts = [c for c in db.calls if "INSERT INTO kyc_events" in c[0]]
    assert any((c[1] or {}).get("ct") == "CA" for c in audit_inserts), \
        "audit row should record country=CA"


def test_webhook_requires_input_event():
    evt = _mk_event("identity.verification_session.requires_input", {
        "id": "vs_TEST3",
        "metadata": {"user_id": "u-3"},
    })
    result, db = _call_webhook(evt)
    assert result["status"] == "ok"
    ri = [c for c in db.calls if "kyc_status='requires_input'" in c[0]]
    assert ri, "expected requires_input UPDATE"


def test_webhook_canceled_event():
    evt = _mk_event("identity.verification_session.canceled", {
        "id": "vs_TEST4",
        "metadata": {"user_id": "u-4"},
    })
    result, db = _call_webhook(evt)
    assert result["status"] == "ok"
    failed = [c for c in db.calls if "kyc_status='failed'" in c[0]]
    assert failed, "expected failed UPDATE for canceled"


def test_webhook_processing_event_keeps_pending():
    evt = _mk_event("identity.verification_session.processing", {
        "id": "vs_TEST5",
        "metadata": {"user_id": "u-5"},
    })
    result, db = _call_webhook(evt)
    assert result["status"] == "ok"
    # SQL should set kyc_status='pending'
    pending = [c for c in db.calls if "kyc_status='pending'" in c[0]]
    assert pending, "expected pending UPDATE for processing event"
    audit = [c for c in db.calls if "INSERT INTO kyc_events" in c[0]]
    assert audit, "expected audit insert for processing event"


def test_webhook_handles_stripeobject_not_dict():
    """The exact production bug: event["data"]["object"] is a StripeObject
    whose .get() raises AttributeError. The fixed handler must use
    to_dict_recursive() and extract fields without crashing."""
    evt = _mk_event("identity.verification_session.verified", {
        "id": "vs_REPRO_BUG",
        "metadata": {"user_id": "u-bug"},
        "verified_outputs": {"address": {"country": "US"}},
    }, as_stripe_object=True)
    # Sanity check: confirm the StripeObject still raises on .get (the prod bug)
    with pytest.raises(AttributeError):
        evt.data.object.get("id")
    # Now run the actual handler — must not raise.
    result, db = _call_webhook(evt)
    assert result["status"] == "ok"
    assert result["session_id"] == "vs_REPRO_BUG"
    verified = [c for c in db.calls if "kyc_status='verified'" in c[0]]
    assert verified, "verified UPDATE must run even with StripeObject input"


def test_webhook_invalid_signature_returns_400():
    """construct_event raises SignatureVerificationError -> handler returns 400 (not 500)."""
    from app.api.routes import kyc as kyc_mod
    from fastapi import HTTPException

    fake_stripe = MagicMock()
    class _SigErr(Exception): pass
    fake_stripe.error = types.SimpleNamespace(SignatureVerificationError=_SigErr)
    fake_stripe.Webhook.construct_event.side_effect = _SigErr("bad sig")

    req = FakeRequest(body=b"{}", headers={"stripe-signature": "bad"})
    db = FakeAsyncSession()
    with patch.dict(sys.modules, {"stripe": fake_stripe}):
        with pytest.raises(HTTPException) as exc:
            asyncio.new_event_loop().run_until_complete(kyc_mod.kyc_webhook(req, db))
    assert exc.value.status_code == 400
    assert "signature" in exc.value.detail.lower() or "invalid" in exc.value.detail.lower()


def test_webhook_missing_secret_returns_503(monkeypatch):
    """STRIPE_IDENTITY_WEBHOOK_SECRET unset -> 503 (config error, not 500)."""
    from app.api.routes import kyc as kyc_mod
    from fastapi import HTTPException
    monkeypatch.delenv("STRIPE_IDENTITY_WEBHOOK_SECRET", raising=False)
    monkeypatch.setattr(kyc_mod, "STRIPE_IDENTITY_KEY", "sk_test_dummy")
    req = FakeRequest(body=b"{}", headers={"stripe-signature": "t=1,v1=x"})
    db = FakeAsyncSession()
    with pytest.raises(HTTPException) as exc:
        asyncio.new_event_loop().run_until_complete(kyc_mod.kyc_webhook(req, db))
    assert exc.value.status_code == 503
