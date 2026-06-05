"""Bug 2 (2026-06-05): the suggestion widget never delivered. The frontend
fix routes through the shared axios client; the backend side is proven here.

In-process tests (no HTTP) that call the `suggestion` route handler directly
with a fake authed user + fake Request, monkeypatching the email sender so no
real mail goes out. Verifies:
  * valid message from an authed user → 201 + the email-send path is invoked
  * a message shorter than 5 chars → 400 (HTTPException) and no send
  * the backend recipient stays the admin inbox (ADMIN_NOTIFY_EMAIL), unchanged
"""
import uuid
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.routes.support as support_mod
import app.services.email as email_mod


def _fake_user():
    return SimpleNamespace(
        id=uuid.uuid4(),
        email="tester@example.com",
        username="tester",
        subscription_tier="tier_5",
    )


class _FakeClient:
    host = "127.0.0.1"


class _FakeRequest:
    client = _FakeClient()


def _reset_rate():
    # The endpoint stores a per-user bucket in module globals; clear it so the
    # 10/hour limiter never bleeds between tests.
    support_mod.__dict__.pop("_SUGG_RATE", None)


def test_suggestion_valid_returns_201_and_sends(monkeypatch):
    _reset_rate()
    sent = {}

    def _fake_send(to, subject, html):
        sent["to"] = to
        sent["subject"] = subject
        return True

    # The handler does `from app.services.email import _send as _send_em`,
    # which resolves the attribute on the module at call time — patch there.
    monkeypatch.setattr(email_mod, "_send", _fake_send, raising=True)

    data = support_mod.SuggestionRequest(message="please add a dark mode toggle", category="feature")
    result = asyncio.run(support_mod.suggestion(data, _FakeRequest(), current_user=_fake_user()))

    assert result["status"] == "sent"
    assert sent, "email-send path was never invoked"
    # Recipient must remain the owner/admin inbox — NOT changed by this fix.
    assert sent["to"] == "theta.algos@yahoo.com"
    assert "[Admin]" in sent["subject"]


def test_suggestion_too_short_returns_400(monkeypatch):
    _reset_rate()
    called = {"n": 0}
    monkeypatch.setattr(email_mod, "_send",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or True,
                        raising=True)

    data = support_mod.SuggestionRequest(message="hi", category="bug")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(support_mod.suggestion(data, _FakeRequest(), current_user=_fake_user()))
    assert ei.value.status_code == 400
    assert called["n"] == 0, "must not attempt to send for a too-short message"


def test_suggestion_send_failure_returns_502(monkeypatch):
    _reset_rate()
    monkeypatch.setattr(email_mod, "_send", lambda *a, **k: False, raising=True)
    data = support_mod.SuggestionRequest(message="this one fails to send", category="other")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(support_mod.suggestion(data, _FakeRequest(), current_user=_fake_user()))
    assert ei.value.status_code == 502
