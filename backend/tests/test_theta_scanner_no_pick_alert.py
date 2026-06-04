"""BUG B: if the scan window 6:00-9:50 ET closes without firing on a
weekday, exactly ONE URGENT pipeline_failure_alert fires per trading date.

Subsequent calls on the same date must be no-ops (idempotent).

Run: pytest backend/tests/test_theta_scanner_no_pick_alert.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset_state():
    from app.engines.options import premarket_scheduler as ps
    ps._theta_fired_today = None
    ps._theta_last_scan_min = None
    ps._theta_no_pick_alerted_for_date.clear()
    ps._theta_exception_alerted_for_date.clear()
    yield
    ps._theta_fired_today = None
    ps._theta_last_scan_min = None
    ps._theta_no_pick_alerted_for_date.clear()
    ps._theta_exception_alerted_for_date.clear()


def test_no_pick_alert_fires_once_after_window_close(monkeypatch):
    """At 10:00 ET on a weekday, with nothing fired in Redis, we should
    send exactly one pipeline_failure_alert with reason mentioning 'no pick'.

    Second call same date: no second alert."""
    from app.engines.options import premarket_scheduler as ps

    # 10:00 ET == 14:00 UTC in June (EDT, UTC-4)
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 4, 14, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("datetime.datetime", FixedDT, raising=False)

    sent_alerts: list[dict] = []

    async def _capture_alert(reason, context=None, *, traceback_str=None, recipients=None):
        sent_alerts.append({
            "reason": reason,
            "context": context or {},
            "traceback_str": traceback_str,
        })
        return 1

    monkeypatch.setattr(
        "app.engines.pipeline_alerts.send_pipeline_failure_alert",
        _capture_alert,
    )

    # Force redis.get to return None so we don't think we already fired.
    class _FakeRedis:
        def get(self, k):
            return None
        def set(self, *a, **k):
            return True
    monkeypatch.setattr(
        "redis.Redis.from_url",
        classmethod(lambda cls, *a, **k: _FakeRedis()),
    )

    _run(ps._check_and_run_theta_scanner())

    assert len(sent_alerts) == 1, (
        f"BUG B REGRESSION: expected 1 no-pick alert, got {len(sent_alerts)}: {sent_alerts}"
    )
    assert "no pick" in sent_alerts[0]["reason"].lower() or \
           "no" in sent_alerts[0]["reason"].lower(), (
        f"alert reason should mention 'no pick', got: {sent_alerts[0]['reason']!r}"
    )

    # Idempotency check
    _run(ps._check_and_run_theta_scanner())
    assert len(sent_alerts) == 1, (
        f"alert fired again on second call - not idempotent. Got {len(sent_alerts)} alerts"
    )


def test_no_pick_alert_skips_when_already_fired_via_redis(monkeypatch):
    """If Redis says we DID fire today, no alert should go out even past 9:50 ET."""
    from app.engines.options import premarket_scheduler as ps

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 4, 14, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("datetime.datetime", FixedDT, raising=False)

    sent_alerts: list[dict] = []

    async def _capture_alert(reason, context=None, *, traceback_str=None, recipients=None):
        sent_alerts.append({"reason": reason})
        return 1
    monkeypatch.setattr(
        "app.engines.pipeline_alerts.send_pipeline_failure_alert",
        _capture_alert,
    )

    class _FakeRedis:
        def get(self, k):
            return "running" if "theta_fired" in k else None
        def set(self, *a, **k):
            return True
    monkeypatch.setattr(
        "redis.Redis.from_url",
        classmethod(lambda cls, *a, **k: _FakeRedis()),
    )

    _run(ps._check_and_run_theta_scanner())

    assert len(sent_alerts) == 0, (
        f"alert fired even though Redis says we already fired today; "
        f"got: {sent_alerts}"
    )
