"""Tests the startup-time KYC auto-sync hook in app.main._run_kyc_startup_sync.

Asserts:
  1. ONLY rows with kyc_status='pending' AND kyc_session_id NOT NULL get
     processed (the SQL filter is enforced via a mocked DB that returns a
     hand-built row set; non-matching rows do NOT trigger sync_kyc_status_from_stripe).
  2. Per-user exceptions are isolated — one bad row never blocks the rest.
  3. Idempotency — calling the helper twice in the same process re-uses the
     module-level flag and the second call does nothing.

Run standalone:
    pytest backend/tests/test_kyc_startup_sync.py -v -p no:cacheprovider
"""
import asyncio
import pytest
from unittest.mock import patch, MagicMock


class _Mapping(dict):
    def mappings(self): return self
    def all(self): return self._rows
    def __init__(self, rows):
        super().__init__()
        self._rows = rows


class _Result:
    def __init__(self, rows): self._rows = rows
    def mappings(self): return self
    def all(self): return self._rows


class FakeSession:
    def __init__(self, rows): self.rows = rows; self.sqls = []
    async def execute(self, stmt, params=None):
        self.sqls.append(str(getattr(stmt, "text", stmt)))
        return _Result(self.rows)
    async def commit(self): pass
    async def rollback(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_only_pending_with_session_id_get_synced(monkeypatch):
    import app.main as main_mod
    # Reset the once-per-startup flag for this test.
    main_mod._KYC_STARTUP_SYNC_RAN = False
    # The SQL filter is verified by inspecting captured SQL — the rows we
    # hand back are what the SQL would actually return, i.e. ONLY the
    # pending + has-session rows.
    rows = [
        {"id": "u1", "email": "lukasz@x.test", "kyc_status": "pending",
         "kyc_session_id": "vs_lukasz"},
        {"id": "u2", "email": "bob@x.test", "kyc_status": "pending",
         "kyc_session_id": "vs_bob"},
    ]
    sess = FakeSession(rows)
    factory = MagicMock(return_value=sess)
    synced_for: list[tuple[str, str]] = []
    async def fake_sync(db, *, user_id, session_id):
        synced_for.append((user_id, session_id))
        return "verified"
    with patch("app.database.async_session_factory", new=factory), \
         patch("app.api.routes.kyc.sync_kyc_status_from_stripe", new=fake_sync):
        _run(main_mod._run_kyc_startup_sync())
    # SQL must filter on pending + non-null session
    assert any("kyc_status = 'pending'" in s for s in sess.sqls)
    assert any("kyc_session_id IS NOT NULL" in s for s in sess.sqls)
    # Both users got synced
    assert sorted(synced_for) == [("u1", "vs_lukasz"), ("u2", "vs_bob")]


def test_per_user_errors_isolated(monkeypatch):
    import app.main as main_mod
    main_mod._KYC_STARTUP_SYNC_RAN = False
    rows = [
        {"id": "u1", "email": "good1@x.test", "kyc_status": "pending",
         "kyc_session_id": "vs_1"},
        {"id": "u2", "email": "bad@x.test", "kyc_status": "pending",
         "kyc_session_id": "vs_2"},
        {"id": "u3", "email": "good2@x.test", "kyc_status": "pending",
         "kyc_session_id": "vs_3"},
    ]
    sess = FakeSession(rows)
    factory = MagicMock(return_value=sess)
    processed: list[str] = []
    async def fake_sync(db, *, user_id, session_id):
        processed.append(user_id)
        if user_id == "u2":
            raise RuntimeError("Stripe brownout for u2")
        return "verified"
    with patch("app.database.async_session_factory", new=factory), \
         patch("app.api.routes.kyc.sync_kyc_status_from_stripe", new=fake_sync):
        _run(main_mod._run_kyc_startup_sync())
    # u1 + u3 still got their sync attempted DESPITE u2 raising mid-sweep
    assert processed == ["u1", "u2", "u3"], \
        f"per-user failure must not abort the loop; processed={processed}"


def test_idempotent_within_process(monkeypatch):
    import app.main as main_mod
    main_mod._KYC_STARTUP_SYNC_RAN = False
    rows = [
        {"id": "u1", "email": "only@x.test", "kyc_status": "pending",
         "kyc_session_id": "vs_x"},
    ]
    sess = FakeSession(rows)
    factory = MagicMock(return_value=sess)
    n_called = {"n": 0}
    async def fake_sync(db, *, user_id, session_id):
        n_called["n"] += 1
        return "verified"
    with patch("app.database.async_session_factory", new=factory), \
         patch("app.api.routes.kyc.sync_kyc_status_from_stripe", new=fake_sync):
        _run(main_mod._run_kyc_startup_sync())
        _run(main_mod._run_kyc_startup_sync())
        _run(main_mod._run_kyc_startup_sync())
    assert n_called["n"] == 1, \
        f"the helper should be a no-op after the first call; got n={n_called['n']}"
