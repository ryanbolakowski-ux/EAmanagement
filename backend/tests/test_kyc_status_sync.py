"""Unit tests for sync_kyc_status_from_stripe in app.api.routes.kyc.

These verify the webhook-loss safety net: pulling authoritative status from
Stripe and reconciling the local row idempotently.

Run standalone with:
    pytest backend/tests/test_kyc_status_sync.py -v -p no:cacheprovider
"""
import sys
import asyncio
import types
import pytest
from unittest.mock import MagicMock, patch


class _FakeResult:
    def __init__(self, rows=None): self._rows = rows or []
    def mappings(self): return self
    def all(self): return self._rows


class FakeAsyncSession:
    def __init__(self):
        self.calls = []
        self.commits = 0
    async def execute(self, stmt, params=None):
        self.calls.append((str(getattr(stmt, "text", stmt)), params or {}))
        return _FakeResult()
    async def commit(self): self.commits += 1
    async def rollback(self): pass


class FakeVS:
    """Mimics stripe.identity.VerificationSession (the StripeObject form)."""
    def __init__(self, **data):
        self.__dict__["_data"] = dict(data)
    def __getattr__(self, name):
        if name in self._data: return self._data[name]
        raise AttributeError(name)


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    from app.api.routes import kyc as kyc_mod
    monkeypatch.setattr(kyc_mod, "STRIPE_IDENTITY_KEY", "sk_test_dummy")


def _run_sync(vs_or_exc):
    """Patch stripe.identity.VerificationSession.retrieve to return vs_or_exc."""
    from app.api.routes import kyc as kyc_mod
    fake_stripe = MagicMock()
    if isinstance(vs_or_exc, BaseException):
        fake_stripe.identity.VerificationSession.retrieve.side_effect = vs_or_exc
    else:
        fake_stripe.identity.VerificationSession.retrieve.return_value = vs_or_exc
    db = FakeAsyncSession()
    with patch.dict(sys.modules, {"stripe": fake_stripe}):
        result = asyncio.new_event_loop().run_until_complete(
            kyc_mod.sync_kyc_status_from_stripe(db, user_id="u-x", session_id="vs_X")
        )
    return result, db


def test_sync_pending_to_verified():
    """Stripe reports verified -> user is set to verified, kyc_verified_at populated."""
    vs = FakeVS(
        status="verified",
        verified_outputs={"address": {"country": "US"}},
    )
    result, db = _run_sync(vs)
    assert result == "verified"
    verified = [c for c in db.calls if "kyc_status='verified'" in c[0]]
    assert verified, f"expected verified UPDATE; got: {[c[0] for c in db.calls]}"
    # COALESCE keeps a verified_at that's already set; otherwise NOW()
    assert any("COALESCE(kyc_verified_at, NOW())" in c[0] for c in verified)


def test_sync_never_downgrades_verified():
    """Stripe reports 'processing' for a user already locally verified -> the
    UPDATE must be guarded so we never downgrade. We verify the SQL contains
    NOT IN ('verified') for the non-verified-state UPDATE path."""
    vs = FakeVS(status="processing")
    result, db = _run_sync(vs)
    assert result == "pending"
    # Critical: the UPDATE must have the NOT IN ('verified') guard
    updates = [c for c in db.calls if "UPDATE users SET kyc_status" in c[0]]
    assert updates, "expected an UPDATE"
    assert all("NOT IN ('verified')" in c[0] for c in updates), \
        "every non-verified UPDATE must guard against downgrading a verified user"


def test_sync_handles_stripe_error_gracefully():
    """Stripe raises -> sync returns None, no SQL modification."""
    result, db = _run_sync(RuntimeError("boom"))
    assert result is None
    update_calls = [c for c in db.calls if "UPDATE users SET kyc_status" in c[0]]
    assert update_calls == [], "no UPDATE should run when Stripe call fails"


def test_sync_canceled_maps_to_failed():
    vs = FakeVS(status="canceled")
    result, db = _run_sync(vs)
    assert result == "failed"
    failed = [c for c in db.calls if "kyc_status" in c[0] and (c[1] or {}).get("st") == "failed"]
    assert failed, "expected UPDATE with st='failed'"
