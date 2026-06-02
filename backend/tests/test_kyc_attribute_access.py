"""Issue 1 verification — KYC webhook must extract session_id + user_id
even when the StripeObject does NOT expose .get/.to_dict_recursive (real
prod state in 2026-06-02 logs: both came back as None even though Stripe
was sending the verified event).

Two scenarios:
  A. StripeObject supports neither .get NOR .to_dict_recursive, only []
     and attribute access (real SDK v9 behavior). _stripe_get must still
     work.
  B. metadata.user_id is MISSING — handler must fall back to a DB lookup
     by kyc_session_id and STILL update the user.

Run:  pytest backend/tests/test_kyc_attribute_access.py -v -p no:cacheprovider
"""
import asyncio
import sys
import types
import pytest
from unittest.mock import MagicMock, patch


# ─── StripeObject simulator that ONLY exposes [] + getattr ──────────────────
class HostileStripeObject:
    """Mirrors the WORST prod shape: no .get, no .to_dict_recursive, no dict().
    The only ways to read a field are obj[key] or obj.key (attr).
    This is exactly the shape that broke prod in 2026-06-02 logs."""
    def __init__(self, data):
        object.__setattr__(self, "_data", dict(data))
    def __getitem__(self, k):
        v = self._data[k]
        if isinstance(v, dict):
            return HostileStripeObject(v)
        return v
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            v = self._data[name]
            if isinstance(v, dict):
                return HostileStripeObject(v)
            return v
        raise AttributeError(name)
    def __contains__(self, k):
        return k in self._data
    # CRUCIALLY: no .get, no .to_dict_recursive, dict(self) -> {}.


class _Mappings:
    def __init__(self, rows):
        self._rows = rows
    def all(self):
        return self._rows


class _FakeResult:
    """A minimal SQLAlchemy result. .first() returns a Row-like obj with .id."""
    def __init__(self, rows):
        self._rows = rows or []
    def mappings(self):
        return _Mappings(self._rows)
    def all(self):
        return self._rows
    def first(self):
        if not self._rows:
            return None
        # Convert dict to attribute-bag
        r = self._rows[0]
        if isinstance(r, dict):
            return types.SimpleNamespace(**r)
        return r


class FakeAsyncSession:
    """Records every executed SQL + params and can return canned rows
    on a per-query-substring basis."""
    def __init__(self, rows_by_substr=None):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self._rows_by_substr = rows_by_substr or {}
    async def execute(self, stmt, params=None):
        sql = str(getattr(stmt, "text", stmt))
        self.calls.append((sql, params or {}))
        for substr, rows in self._rows_by_substr.items():
            if substr in sql:
                return _FakeResult(rows)
        return _FakeResult([])
    async def commit(self): self.commits += 1
    async def rollback(self): self.rollbacks += 1
    async def refresh(self, obj): return None


class FakeRequest:
    def __init__(self, body=b"", headers=None):
        self._body = body
        self.headers = headers or {}
    async def body(self): return self._body


def _build_event(event_type, obj_dict):
    """Build a StripeObject event WITHOUT .get / .to_dict_recursive — the
    actual hostile shape we hit in prod."""
    inner = HostileStripeObject(obj_dict)
    return HostileStripeObject({
        "type": event_type,
        "data": HostileStripeObject({"object": inner}).__dict__["_data"]  # nested attr access works
    })


@pytest.fixture(autouse=True)
def _stripe_env(monkeypatch):
    monkeypatch.setenv("STRIPE_IDENTITY_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("STRIPE_IDENTITY_KEY", "sk_test")
    from app.api.routes import kyc as kyc_mod
    monkeypatch.setattr(kyc_mod, "STRIPE_IDENTITY_KEY", "sk_test", raising=True)


def _call_webhook(event, db, *, body=b"{}", sig="t=1,v1=x"):
    from app.api.routes import kyc as kyc_mod
    req = FakeRequest(body=body, headers={"stripe-signature": sig})
    fake_stripe = MagicMock()
    fake_stripe.error = types.SimpleNamespace(SignatureVerificationError=Exception)
    fake_stripe.Webhook.construct_event.return_value = event
    with patch.dict(sys.modules, {"stripe": fake_stripe}):
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(kyc_mod.kyc_webhook(req, db))
        finally:
            loop.close()
    return result


# ─── HELPER UNIT TEST: _stripe_get against hostile object ───────────────────
def test_stripe_get_handles_object_without_dot_get():
    """The _stripe_get helper must extract values from a StripeObject that
    has NO .get method and would return {} from dict(o)."""
    from app.api.routes.kyc import _stripe_get
    o = HostileStripeObject({"id": "vs_X", "metadata": {"user_id": "u-1"}})
    assert _stripe_get(o, "id") == "vs_X"
    md = _stripe_get(o, "metadata")
    assert md is not None
    assert _stripe_get(md, "user_id") == "u-1"
    # Missing key -> None, not crash
    assert _stripe_get(o, "nope") is None
    # dict path still works
    assert _stripe_get({"a": 1}, "a") == 1
    assert _stripe_get(None, "x") is None


# ─── SCENARIO A: metadata present, attribute access only ────────────────────
def test_webhook_extracts_via_attribute_access_without_get_method():
    """The hostile shape: only [] + attr access work. The fix uses _stripe_get
    so session_id + user_id are still extracted correctly."""
    evt = _build_event("identity.verification_session.verified", {
        "id": "vs_ATTR_TEST",
        "metadata": {"user_id": "u-attr-1"},
        "verified_outputs": {"address": {"country": "US"}},
    })
    db = FakeAsyncSession()
    result = _call_webhook(evt, db)
    assert result["status"] == "ok"
    assert result["session_id"] == "vs_ATTR_TEST", f"session_id extraction failed: {result}"
    # Should have issued an UPDATE setting kyc_status='verified' with user_id=u-attr-1
    update_calls = [c for c in db.calls if "kyc_status='verified'" in c[0]]
    assert update_calls, f"expected verified UPDATE; calls: {[c[0][:80] for c in db.calls]}"
    assert update_calls[0][1].get("uid") == "u-attr-1"
    assert update_calls[0][1].get("sid") == "vs_ATTR_TEST"


# ─── SCENARIO B: missing metadata, falls back to DB lookup by kyc_session_id ─
def test_webhook_falls_back_to_db_lookup_when_user_id_missing():
    """metadata has no user_id (old session, or Stripe stripped it). The
    handler must recover by querying users.kyc_session_id."""
    evt = _build_event("identity.verification_session.verified", {
        "id": "vs_FALLBACK",
        "metadata": {},  # NO user_id
        "verified_outputs": {"address": {"country": "US"}},
    })
    db = FakeAsyncSession(rows_by_substr={
        "FROM users WHERE kyc_session_id": [{"id": "u-recovered-1"}],
    })
    result = _call_webhook(evt, db)
    assert result["status"] == "ok"
    # The recovery query must have been issued
    fallback_calls = [c for c in db.calls if "FROM users WHERE kyc_session_id" in c[0]]
    assert fallback_calls, f"expected DB fallback lookup; calls: {[c[0][:80] for c in db.calls]}"
    # And the subsequent verified UPDATE should use the recovered uid
    update_calls = [c for c in db.calls if "kyc_status='verified'" in c[0]]
    assert update_calls, "expected verified UPDATE after fallback"
    assert update_calls[0][1].get("uid") == "u-recovered-1"


# ─── SCENARIO C: requires_input event also routes via _stripe_get ───────────
def test_webhook_requires_input_event():
    evt = _build_event("identity.verification_session.requires_input", {
        "id": "vs_REQUIRES",
        "metadata": {"user_id": "u-2"},
    })
    db = FakeAsyncSession()
    result = _call_webhook(evt, db)
    assert result["session_id"] == "vs_REQUIRES"
    ri_calls = [c for c in db.calls if "kyc_status='requires_input'" in c[0]]
    assert ri_calls
