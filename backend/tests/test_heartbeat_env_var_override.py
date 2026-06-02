"""ADMIN_HEARTBEAT_EMAIL env var must override the default recipient.

Run standalone:
    pytest backend/tests/test_heartbeat_env_var_override.py -v -p no:cacheprovider
"""
import asyncio
import os
from unittest.mock import patch, MagicMock


class _RowsResult:
    def __init__(self, rows): self._rows = rows
    def fetchall(self): return self._rows
    def scalar(self): return 0
    def first(self): return None


class FakeDB:
    async def execute(self, stmt, params=None):
        return _RowsResult([])
    async def commit(self): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def test_env_var_overrides_default_recipient():
    """With ADMIN_HEARTBEAT_EMAIL='someone-else@example.com' in the env, the
    heartbeat must route to that address, NOT the hardcoded ryan address."""
    db = FakeDB()
    fake_factory = MagicMock(return_value=db)
    async def _fake_health():
        return {"ok": True, "components": {"redis": {"ok": True}}}
    sent_to: list[str] = []
    def _fake_send(to, subj, html):
        sent_to.append(to); return True
    new_target = "someone-else@example.com"
    # Reload scanner_health to re-read the module-level constant under the new
    # env. We patch the constant AND os.environ — the production code re-reads
    # the env at send-time so the dynamic override path is tested too.
    with patch.dict(os.environ, {"ADMIN_HEARTBEAT_EMAIL": new_target}, clear=False):
        from app.engines import scanner_health as sh
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
    assert sent_to == [new_target], \
        f"ADMIN_HEARTBEAT_EMAIL env var must route the send; got {sent_to!r}"
