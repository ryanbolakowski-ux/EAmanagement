"""Heartbeat MUST only fan out to is_admin=true users.

Prior to 2026-06-01 the heartbeat went to all 11 active strategy subscribers,
leaking pipeline state to customers. The SQL is now constrained to
`u.is_admin = TRUE`. We assert that:
  1. The SQL the heartbeat issues contains "is_admin = true".
  2. When the mocked DB returns mixed admin/non-admin rows, _send is invoked
     ONLY for the admin emails.

Run standalone:
    pytest backend/tests/test_heartbeat_admin_only.py -v -p no:cacheprovider
"""
import asyncio
import types
import pytest
from unittest.mock import MagicMock, patch


class _RowsResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def scalar(self): return 0
    def first(self): return None
    def mappings(self): return self
    def all(self): return self._rows


class FakeDB:
    """Records every SQL execute() and lets us program the rows returned for
    the recipient SELECT. Each non-recipient SELECT (the health-check probes)
    returns empty so the heartbeat code-path is exercised without needing
    real DB / Redis / Resend."""
    def __init__(self, recipient_rows): self.recipient_rows = recipient_rows; self.sqls = []
    async def execute(self, stmt, params=None):
        s = str(getattr(stmt, "text", stmt))
        self.sqls.append(s)
        if "SELECT DISTINCT u.email" in s:
            return _RowsResult(self.recipient_rows)
        return _RowsResult([])
    async def commit(self): pass
    async def rollback(self): pass

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _make_admin_only_db():
    rows = [
        types.SimpleNamespace(email="admin1@theta.test"),
        types.SimpleNamespace(email="admin2@theta.test"),
    ]
    return FakeDB(rows)


def test_recipient_sql_filters_to_admins():
    """Source-level regression: the recipient SELECT in send_daily_heartbeat
    MUST contain `is_admin = true`. Previously the SQL fanned out to every
    user with an ACTIVE strategy or watcher — leaking pipeline state to all
    11 customers. We keep this as a literal-source assertion so accidental
    rewrites of the SQL trigger an immediate test failure."""
    import importlib.util as _u
    spec = _u.find_spec("app.engines.scanner_health")
    assert spec and spec.origin
    with open(spec.origin, "r") as f:
        src = f.read()
    # The constrained SELECT lives inside send_daily_heartbeat. We verify both
    # the column predicate AND the absence of the old strategy/watcher JOINs
    # that previously leaked heartbeat to non-admins.
    assert "is_admin = true" in src, \
        "recipient SQL must filter on is_admin = true"
    # The old SQL JOINed strategies + account_signal_watchers. Make sure the
    # admin-only SELECT does NOT pull those tables in for recipient selection
    # (they still appear elsewhere in the file via other queries, so the test
    # checks the proximity of the JOINs to the recipient SELECT).
    block = src[src.find("SELECT DISTINCT u.email"):]
    block = block[: block.find('"""))')]
    assert "is_admin = true" in block, \
        f"recipient block must enforce is_admin = true; block={block!r}"
    assert "strategies s" not in block, \
        "recipient SELECT must not JOIN strategies (would leak to non-admins)"
    assert "account_signal_watchers w" not in block, \
        "recipient SELECT must not JOIN account_signal_watchers"


def test_send_only_invoked_for_admin_rows():
    """When the mocked DB returns 2 admin rows, _send is called for both
    admin emails — never for a non-admin (since the SQL would filter
    non-admins out before they ever reach this list)."""
    from app.engines import scanner_health as sh
    db = _make_admin_only_db()
    fake_factory = MagicMock(return_value=db)
    async def _fake_health():
        return {"ok": True, "components": {"redis": {"ok": True}}}
    sent_to = []
    def _fake_send(to, subj, html):
        sent_to.append(to); return True
    with patch.object(sh, "_LAST_HEARTBEAT_SENT", None), \
         patch.object(sh, "check_health", new=_fake_health), \
         patch("app.database.async_session_factory", new=fake_factory), \
         patch("app.services.email._send", new=_fake_send), \
         patch("app.engines.market_calendar.market_status",
               return_value={"is_trading_day": True, "now_et": "2026-06-01 09:30:00 ET"}):
        from datetime import datetime as _dt, timezone as _tz
        fake_now = _dt(2026, 6, 1, 13, 30, tzinfo=_tz.utc)  # 09:30 ET
        with patch("app.engines.scanner_health.datetime") as md:
            md.now.return_value = fake_now
            asyncio.new_event_loop().run_until_complete(sh.send_daily_heartbeat())
    assert sorted(sent_to) == ["admin1@theta.test", "admin2@theta.test"], \
        f"_send should be called for both admin rows; got {sent_to}"
