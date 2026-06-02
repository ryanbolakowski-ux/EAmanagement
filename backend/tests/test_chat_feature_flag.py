"""Tests for the AI-chat feature flag (CHAT_ENABLED in routes/support.py).

The flag is ENABLE_AI_CHAT in the environment. When it's false (the default
on prod for now), the chat endpoints MUST:

  1. /chat/status returns {configured: False, disabled: True, message: ...} 200
  2. POST /chat returns 503 immediately with a friendly detail
  3. The anthropic SDK is NEVER imported in the disabled code path (so we
     don't even pay the import cost, let alone make API calls)

Run with: pytest backend/tests/test_chat_feature_flag.py -v -p no:cacheprovider

These tests run in-process by re-importing the support module with a controlled
environment, so they validate the patched source code in this worktree even
when the running prod backend hasn't picked up the patch yet.
"""
from __future__ import annotations

import importlib
import os
import sys
from unittest.mock import patch

import pytest


def _reimport_support_module(enable_chat: bool):
    """Re-import app.api.routes.support with ENABLE_AI_CHAT set to `enable_chat`,
    returning the freshly-loaded module."""
    os.environ["ENABLE_AI_CHAT"] = "true" if enable_chat else "false"
    for mod in [m for m in list(sys.modules) if m.startswith("app.api.routes.support")]:
        sys.modules.pop(mod, None)
    return importlib.import_module("app.api.routes.support")


@pytest.fixture(autouse=True)
def _isolated_env():
    """Snapshot + restore env around every test so re-imports never leak state."""
    snap = os.environ.get("ENABLE_AI_CHAT")
    snap_modules = {k: v for k, v in sys.modules.items() if k.startswith("app.api.routes.support")}
    yield
    if snap is None:
        os.environ.pop("ENABLE_AI_CHAT", None)
    else:
        os.environ["ENABLE_AI_CHAT"] = snap
    for mod in [m for m in list(sys.modules) if m.startswith("app.api.routes.support")]:
        sys.modules.pop(mod, None)
    for k, v in snap_modules.items():
        sys.modules[k] = v


def test_chat_module_constant_reads_env_var():
    """CHAT_ENABLED is set at import time from ENABLE_AI_CHAT."""
    mod_off = _reimport_support_module(False)
    assert mod_off.CHAT_ENABLED is False, "expected CHAT_ENABLED False when env var is 'false'"

    mod_on = _reimport_support_module(True)
    assert mod_on.CHAT_ENABLED is True, "expected CHAT_ENABLED True when env var is 'true'"


def test_chat_status_when_disabled():
    """Direct call to chat_status() returns the disabled-shape payload."""
    import asyncio
    mod = _reimport_support_module(False)
    assert mod.CHAT_ENABLED is False

    class _FakeUser:
        id = "test-user"
        email = "test@thetaalgos.test"
        username = "test_user"
        subscription_tier = "tier_5"

    body = asyncio.run(mod.chat_status(_FakeUser()))
    assert body == {
        "configured": False,
        "disabled": True,
        "message": "AI chat is temporarily disabled. Visit /help for the FAQ.",
    }


def test_chat_status_when_enabled_does_not_short_circuit():
    """When CHAT_ENABLED is true, chat_status() falls back to the
    configured/disabled=False shape and reads ANTHROPIC_API_KEY."""
    import asyncio
    os.environ.pop("ANTHROPIC_API_KEY", None)
    mod = _reimport_support_module(True)
    assert mod.CHAT_ENABLED is True

    class _FakeUser:
        id = "test-user"
        email = "test@thetaalgos.test"
        username = "test_user"
        subscription_tier = "tier_5"

    body = asyncio.run(mod.chat_status(_FakeUser()))
    assert body == {"configured": False, "disabled": False}


def test_chat_endpoint_when_disabled_returns_503():
    """Direct call to chat() raises HTTPException(503) when the flag is off."""
    import asyncio
    from fastapi import HTTPException

    mod = _reimport_support_module(False)
    assert mod.CHAT_ENABLED is False

    class _FakeReq:
        client = type("C", (), {"host": "127.0.0.1"})()

    class _FakeUser:
        id = "test-user"
        email = "test@thetaalgos.test"
        username = "test_user"
        subscription_tier = "tier_5"

    data = mod._ChatRequest(messages=[mod._ChatMessage(role="user", content="hello")])

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(mod.chat(data, _FakeReq(), _FakeUser()))

    assert exc_info.value.status_code == 503
    detail = exc_info.value.detail
    assert "disabled" in detail.lower()
    assert "/help" in detail or "thetaalgos.com/help" in detail


def test_chat_endpoint_never_imports_anthropic_when_disabled():
    """When ENABLE_AI_CHAT is false, calling the chat() handler MUST raise the
    503 BEFORE any 'from anthropic import AsyncAnthropic' statement runs.

    We sentinel-stub sys.modules['anthropic'] so any import attempt raises an
    AssertionError; then we drive the handler and assert nothing was touched.
    """
    import asyncio
    from fastapi import HTTPException

    mod = _reimport_support_module(False)
    assert mod.CHAT_ENABLED is False

    accessed = {"hit": False}

    class _Sentinel:
        def __getattr__(self, name):
            accessed["hit"] = True
            raise AssertionError(
                f"anthropic SDK touched while CHAT_ENABLED=False (attr={name!r})"
            )

    with patch.dict(sys.modules, {"anthropic": _Sentinel()}):
        class _FakeReq:
            client = type("C", (), {"host": "127.0.0.1"})()

        class _FakeUser:
            id = "test-user"
            email = "test@thetaalgos.test"
            username = "test_user"
            subscription_tier = "tier_5"

        data = mod._ChatRequest(messages=[mod._ChatMessage(role="user", content="hello")])

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(mod.chat(data, _FakeReq(), _FakeUser()))

        assert exc_info.value.status_code == 503
        assert accessed["hit"] is False, (
            "anthropic SDK was accessed in the disabled path - feature flag failed!"
        )
