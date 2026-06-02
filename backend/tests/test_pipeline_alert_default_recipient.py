"""send_pipeline_failure_alert() with no `recipients=` arg must default to
ADMIN_HEARTBEAT_EMAIL (single address) — never the DB admin fan-out.

Run standalone:
    pytest backend/tests/test_pipeline_alert_default_recipient.py -v -p no:cacheprovider
"""
import asyncio
import os
from unittest.mock import patch


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_default_recipient_is_admin_heartbeat_email():
    from app.engines import pipeline_alerts as pa
    captured: list[str] = []
    def fake_send_tracked(to, subj, html):
        captured.append(to)
        return {"sent": True, "provider_message_id": "x", "provider_status": "sent",
                "error": None, "latency_ms": 12}
    # We do NOT patch _fetch_admin_emails — the new code path should NOT call
    # it when no recipients= is supplied. If it does, the test still passes
    # only when fan-out returns the same single email, which it can't (real
    # DB or empty fallback).
    with patch("app.services.email._send_tracked", new=fake_send_tracked):
        n = _run(pa.send_pipeline_failure_alert(
            "test reason",
            context={"job": "pytest"},
        ))
    expected = os.environ.get("ADMIN_HEARTBEAT_EMAIL", "ryan.bolakowski@icloud.com")
    assert captured == [expected], \
        f"default recipient must be {expected!r}; got {captured!r}"
    assert n == 1, f"expected 1 send, got {n}"


def test_explicit_recipients_override_default():
    """Override path: when callers pass `recipients=` the function honors it."""
    from app.engines import pipeline_alerts as pa
    captured: list[str] = []
    def fake_send_tracked(to, subj, html):
        captured.append(to)
        return {"sent": True, "provider_message_id": "x", "provider_status": "sent",
                "error": None, "latency_ms": 12}
    with patch("app.services.email._send_tracked", new=fake_send_tracked):
        n = _run(pa.send_pipeline_failure_alert(
            "override test",
            context={"job": "pytest"},
            recipients=["override@theta.test", "second@theta.test"],
        ))
    assert sorted(captured) == ["override@theta.test", "second@theta.test"], \
        f"override list must be used verbatim; got {captured!r}"
    assert n == 2
