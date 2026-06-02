"""Heartbeat MUST go to ADMIN_HEARTBEAT_EMAIL (single recipient) — never to
the DB-wide admin list. Regression test for the 2026-06-03 tightening.

Run standalone:
    pytest backend/tests/test_heartbeat_single_recipient.py -v -p no:cacheprovider
"""
import asyncio
import types
from unittest.mock import patch, MagicMock


class _RowsResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def scalar(self): return 0
    def first(self): return None


class FakeDB:
    """Returns empty rows everywhere — heartbeat must no longer use DB SELECT
    to pick recipients. If the implementation still queries for recipients the
    fake DB will simply return [] and the assertion below will catch it."""
    def __init__(self): self.sqls = []
    async def execute(self, stmt, params=None):
        self.sqls.append(str(getattr(stmt, "text", stmt)))
        return _RowsResult([])
    async def commit(self): pass
    async def rollback(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def test_heartbeat_sends_to_single_admin_email_only():
    from app.engines import scanner_health as sh
    db = FakeDB()
    fake_factory = MagicMock(return_value=db)
    async def _fake_health():
        return {"ok": True, "components": {"redis": {"ok": True}}}
    sent_to: list[str] = []
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
    # Heartbeat must be called for exactly ONE recipient = ADMIN_HEARTBEAT_EMAIL.
    assert sent_to == [sh.ADMIN_HEARTBEAT_EMAIL], \
        f"expected single send to ADMIN_HEARTBEAT_EMAIL, got {sent_to!r}"
    # Default constant resolves to ryan.bolakowski@icloud.com when the env is
    # absent in tests (conftest does not set ADMIN_HEARTBEAT_EMAIL).
    import os as _os
    if not _os.environ.get("ADMIN_HEARTBEAT_EMAIL"):
        assert sent_to == ["ryan.bolakowski@icloud.com"], \
            f"default recipient must be ryan.bolakowski@icloud.com, got {sent_to!r}"
