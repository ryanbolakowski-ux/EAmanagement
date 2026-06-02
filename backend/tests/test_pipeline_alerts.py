"""Unit tests for app.engines.pipeline_alerts.

Asserts:
  1. Subject of the dispatched email begins with the URGENT marker.
  2. The body HTML embeds the context dict so admins see the failure shape.
  3. The function fails OPEN — if _send_tracked raises, the alert never
     re-raises into the caller (the caller is already in an except: clause).
  4. Returns 0 (not exception) when there are no admin recipients.

Run standalone:
    pytest backend/tests/test_pipeline_alerts.py -v -p no:cacheprovider
"""
import asyncio
import pytest
from unittest.mock import patch, MagicMock


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_subject_starts_with_urgent_and_body_has_context():
    from app.engines import pipeline_alerts as pa
    captured = []
    def fake_send_tracked(to, subj, html):
        captured.append({"to": to, "subj": subj, "html": html})
        return {"sent": True, "provider_message_id": "x", "provider_status": "sent",
                "error": None, "latency_ms": 12}
    async def fake_admins(): return ["admin@theta.test"]
    with patch.object(pa, "_fetch_admin_emails", new=fake_admins), \
         patch("app.services.email._send_tracked", new=fake_send_tracked):
        n = _run(pa.send_pipeline_failure_alert(
            "Heartbeat detected dead component",
            context={"job": "scanner_health.send_daily_heartbeat",
                     "broken_components": ["redis", "polygon"]},
        ))
    assert n == 1, f"expected exactly 1 admin emailed, got {n}"
    assert captured, "alert should have invoked _send_tracked"
    assert captured[0]["subj"].startswith("\U0001F6A8 URGENT"), \
        f"subject must start with URGENT marker, got {captured[0]['subj']!r}"
    html = captured[0]["html"]
    assert "scanner_health.send_daily_heartbeat" in html, \
        "body must embed context key/value (job=...)"
    assert "redis" in html and "polygon" in html, \
        "body must include the broken components list"


def test_fails_open_when_send_tracked_raises():
    """If _send_tracked blows up on every admin, send_pipeline_failure_alert
    must NEVER raise. It is itself called from an except: clause."""
    from app.engines import pipeline_alerts as pa
    def boom(*a, **k): raise RuntimeError("resend down")
    async def fake_admins(): return ["a@x.test", "b@x.test"]
    with patch.object(pa, "_fetch_admin_emails", new=fake_admins), \
         patch("app.services.email._send_tracked", new=boom):
        # Must complete normally and return 0.
        n = _run(pa.send_pipeline_failure_alert(
            "outer scheduler crashed",
            context={"job": "premarket_scheduler.start_premarket_scheduler"},
        ))
    assert n == 0, "0 sends should be counted when every send raises"


def test_returns_zero_when_explicit_recipients_is_empty():
    """Caller can pass `recipients=[]` to suppress the default-recipient
    fan-out. In that case no admins get emailed and the function returns 0.

    The pre-2026-06-03 version of this test asserted that mocking
    `_fetch_admin_emails()` to return [] caused zero sends — but that test
    no longer reflects how the function works. The default path now uses
    the single ADMIN_HEARTBEAT_EMAIL constant and never calls
    `_fetch_admin_emails()` at all, so the only way to enforce a no-send
    is via the explicit empty-list override."""
    from app.engines import pipeline_alerts as pa
    n = _run(pa.send_pipeline_failure_alert("anything",
                                              context={"k": "v"},
                                              recipients=[]))
    assert n == 0, f"explicit recipients=[] must short-circuit to 0; got {n}"
