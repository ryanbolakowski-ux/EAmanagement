"""Heartbeat MUST go to one configured admin email only.

History
-------
* 2026-06-01: tightened the heartbeat from "all 11 active users with a
  strategy subscription" to "every is_admin=true user" — this stopped the
  leak of pipeline state to customers.
* 2026-06-03: tightened further to a SINGLE configured admin recipient
  (``ADMIN_HEARTBEAT_EMAIL`` env, defaulting to
  ``ryan.bolakowski@icloud.com``). The DB-side recipient query was removed
  entirely. This file keeps the original test name for git-log traceability
  but its asserts now reflect the single-recipient contract; the more
  specific single-recipient + env-override checks live in
  ``test_heartbeat_single_recipient.py`` and ``test_heartbeat_env_var_override.py``.

Run standalone:
    pytest backend/tests/test_heartbeat_admin_only.py -v -p no:cacheprovider
"""
import asyncio
from unittest.mock import patch, MagicMock


class _RowsResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def scalar(self): return 0
    def first(self): return None


class FakeDB:
    """All execute() calls return empty results — heartbeat no longer queries
    the DB for recipients, so this is just here to satisfy the health-check
    probes that still touch the DB."""
    async def execute(self, stmt, params=None):
        return _RowsResult([])
    async def commit(self): pass
    async def rollback(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def test_recipient_is_single_admin_email():
    """Source-level regression: the heartbeat must NOT issue a recipient
    SELECT — the multi-admin SQL was removed in favor of a single hardcoded
    address. We assert the function defines ADMIN_HEARTBEAT_EMAIL and does
    NOT contain the old JOINs that previously leaked to non-admins."""
    import importlib.util as _u
    spec = _u.find_spec("app.engines.scanner_health")
    assert spec and spec.origin
    with open(spec.origin, "r") as f:
        src = f.read()
    assert "ADMIN_HEARTBEAT_EMAIL" in src, \
        "module must define ADMIN_HEARTBEAT_EMAIL constant"
    # The old code JOINed strategies + account_signal_watchers to fan out the
    # heartbeat to subscribers. Those JOINs must not appear inside the body
    # of send_daily_heartbeat (still legal elsewhere in the file for the
    # active-watchers probe, but not in the recipient-resolution block).
    func_start = src.find("async def send_daily_heartbeat()")
    assert func_start > 0, "send_daily_heartbeat function not found"
    func_body = src[func_start: func_start + 4000]
    assert "strategies s" not in func_body, \
        "send_daily_heartbeat must not JOIN strategies (would leak to non-admins)"
    assert "account_signal_watchers w" not in func_body, \
        "send_daily_heartbeat must not JOIN account_signal_watchers"


def test_send_invoked_for_exactly_one_recipient():
    """When send_daily_heartbeat runs, _send is called with one address only
    — the single configured ADMIN_HEARTBEAT_EMAIL. No DB-derived rows allowed."""
    from app.engines import scanner_health as sh
    db = FakeDB()
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
               return_value={"is_trading_day": True, "now_et": "2026-06-03 09:30:00 ET"}):
        from datetime import datetime as _dt, timezone as _tz
        fake_now = _dt(2026, 6, 3, 13, 30, tzinfo=_tz.utc)  # 09:30 ET
        with patch("app.engines.scanner_health.datetime") as md:
            md.now.return_value = fake_now
            asyncio.new_event_loop().run_until_complete(sh.send_daily_heartbeat())
    assert len(sent_to) == 1, \
        f"_send must be called exactly once (single-recipient); got {sent_to!r}"
    assert sent_to[0] == sh.ADMIN_HEARTBEAT_EMAIL, \
        f"recipient must be ADMIN_HEARTBEAT_EMAIL; got {sent_to[0]!r}"
