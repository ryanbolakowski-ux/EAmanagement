"""Unit tests for the email killswitch whitelist + the [trade-audit] log.

Run standalone:
    pytest backend/tests/test_email_killswitch_and_audit.py -v -p no:cacheprovider

Covers:
  - "Daily summary - Thu, Jun 4, 2026 - -660.00 P&L" passes the killswitch
    (the legacy whitelist used to drop this and the user saw no daily P&L
    email; this is the regression we're guarding against).
  - Every call to _send_tracked writes a `[trade-audit]` log line with the
    decision + reason + to + subject fields so admins can grep one pattern
    to reconstruct what happened.
"""
import logging
import os
import re


def _stub_resend_ok():
    """Patch httpx.post and resend so _send_tracked thinks the send succeeded
    without ever leaving the box. Returns the captured outbound payload."""
    captured = {}

    class _StubResp:
        status_code = 200
        text = ""
        def json(self):
            return {"id": "stub-resend-id"}

    def _post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _StubResp()

    import httpx
    return _post, captured


def test_daily_summary_passes_killswitch(monkeypatch):
    """The user's daily P&L summary email should be delivered even when
    EMAIL_KILL_SWITCH=1 is set. Subject the prod scheduler ships looks like:
        'Daily summary - Thu, Jun 4, 2026 - -660.00 P&L'
    """
    monkeypatch.setenv("EMAIL_KILL_SWITCH", "1")
    # Stub config.settings.RESEND_API_KEY so the path enters the send branch.
    from app.services import email as email_mod
    monkeypatch.setattr(email_mod.settings, "RESEND_API_KEY", "stub-key", raising=False)

    _post, captured = _stub_resend_ok()
    monkeypatch.setattr("httpx.post", _post, raising=False)
    # _send_tracked imports httpx locally as `_httpx_es`; patch via the module dict.
    import httpx
    monkeypatch.setattr(httpx, "post", _post, raising=False)

    result = email_mod._send_tracked(
        to="user@example.com",
        subject="Daily summary - Thu, Jun 4, 2026 - -660.00 P&L",
        html="<p>...</p>",
    )
    assert isinstance(result, dict), "wrapper must return the dict shape callers expect"
    assert result["sent"] is True, f"daily summary must pass the killswitch; got {result!r}"
    assert result["provider_status"] == "sent"


def test_signal_email_audit_log(caplog, monkeypatch):
    """_send_tracked must emit exactly one [trade-audit] log line per call
    with the structured fields: decision, to, subject, reason."""
    monkeypatch.delenv("EMAIL_KILL_SWITCH", raising=False)
    from app.services import email as email_mod
    monkeypatch.setattr(email_mod.settings, "RESEND_API_KEY", "stub-key", raising=False)

    _post, _ = _stub_resend_ok()
    import httpx
    monkeypatch.setattr(httpx, "post", _post, raising=False)

    # Capture both stdlib logging AND loguru output. loguru forwards to
    # a stderr sink by default; in tests we install a sink that captures
    # records into a list we can search.
    from loguru import logger as _lg
    seen = []
    sink_id = _lg.add(lambda msg: seen.append(str(msg)), level="INFO")
    try:
        result = email_mod._send_tracked(
            to="user@example.com",
            subject="Theta Scanner - LONG ES @ 7544.00",
            html="<p>signal body</p>",
            signal_id="abcd-1234",
        )
    finally:
        _lg.remove(sink_id)

    assert result["sent"] is True
    audit_lines = [s for s in seen if "[trade-audit]" in s]
    assert audit_lines, (
        "no [trade-audit] log line was emitted; the wrapper must record "
        f"every decision so admins can grep one pattern. seen={seen!r}"
    )
    line = audit_lines[-1]
    # Required structured fields.
    assert "decision=sent" in line, line
    assert "to=user@example.com" in line, line
    assert "reason=ok" in line, line
    assert "signal_id=abcd-1234" in line, line
    # Subject was passed through (truncated to <=60 chars per spec).
    assert "subject=" in line, line


def test_audit_log_records_killswitch_drops(caplog, monkeypatch):
    """When the killswitch drops a non-whitelisted subject, the audit log
    must still capture it so admins can see what was suppressed."""
    monkeypatch.setenv("EMAIL_KILL_SWITCH", "1")
    from app.services import email as email_mod

    from loguru import logger as _lg
    seen = []
    sink_id = _lg.add(lambda msg: seen.append(str(msg)), level="INFO")
    try:
        result = email_mod._send_tracked(
            to="x@example.com",
            subject="Some non-whitelisted random subject from a dev script",
            html="<p>hi</p>",
        )
    finally:
        _lg.remove(sink_id)

    assert result["sent"] is False
    assert result["provider_status"] == "killswitch_dropped"
    audit = [s for s in seen if "[trade-audit]" in s]
    assert audit, f"killswitch drop should still emit a [trade-audit] line; seen={seen!r}"
    assert "decision=dropped" in audit[-1], audit[-1]
    assert "reason=killswitch_dropped" in audit[-1], audit[-1]
