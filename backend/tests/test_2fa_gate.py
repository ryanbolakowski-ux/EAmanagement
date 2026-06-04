"""Unit tests for the 2FA gate dependency `require_2fa_when_paid`.

Run standalone:
    pytest backend/tests/test_2fa_gate.py -v -p no:cacheprovider

Covers:
  - paid user without 2FA -> 403 detail.code='requires_2fa_setup'
  - active-trial user without 2FA -> 403
  - free user without trial -> 200 (gate is open)
  - paid user WITH 2FA -> 200
  - paid user with expired trial -> 200 (gate opens once trial expires)
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.core.auth import require_2fa_when_paid


class _UserStub:
    """Minimal stand-in for app.models.user.User; only attributes the
    gate dependency touches."""
    def __init__(
        self,
        totp_enabled: bool = False,
        subscription_tier: str = "free",
        trial_started_at=None,
        trial_ends_at=None,
    ):
        self.totp_enabled = totp_enabled
        self.subscription_tier = subscription_tier
        self.trial_started_at = trial_started_at
        self.trial_ends_at = trial_ends_at


def _call(user):
    """Invoke the async dep synchronously since the body never awaits a DB."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(require_2fa_when_paid(user))
    finally:
        loop.close()


def _utc_now():
    return datetime.now(timezone.utc)


def test_2fa_required_for_paid_users():
    """A user on tier_5 with totp_enabled=False must be blocked."""
    user = _UserStub(totp_enabled=False, subscription_tier="tier_5")
    with pytest.raises(HTTPException) as ei:
        _call(user)
    assert ei.value.status_code == 403
    assert isinstance(ei.value.detail, dict), "detail must be the structured dict so frontend can branch on detail.code"
    assert ei.value.detail.get("code") == "requires_2fa_setup", ei.value.detail


def test_2fa_required_for_trial_users():
    """Active trial user without 2FA is also gated."""
    user = _UserStub(
        totp_enabled=False,
        subscription_tier="free_trial",
        trial_started_at=_utc_now() - timedelta(days=2),
        trial_ends_at=_utc_now() + timedelta(days=28),
    )
    with pytest.raises(HTTPException) as ei:
        _call(user)
    assert ei.value.status_code == 403
    assert ei.value.detail.get("code") == "requires_2fa_setup"


def test_2fa_not_required_for_free():
    """Free user with NO trial (never started one); gate is open."""
    user = _UserStub(totp_enabled=False, subscription_tier="free")
    out = _call(user)
    assert out is user, "free user should pass through unchanged"


def test_2fa_not_required_when_enabled():
    """Paid user with totp_enabled=True; gate is open even though tier is paid."""
    user = _UserStub(totp_enabled=True, subscription_tier="tier_4")
    out = _call(user)
    assert out is user


def test_2fa_gate_on_expired_trial():
    """Trial that ended yesterday + user is on the free tier and has not
    upgraded. The gate should be OPEN (don't punish users whose trial just
    expired). We also leave totp_enabled alone; never auto-mutate user
    state from a request dependency."""
    user = _UserStub(
        totp_enabled=False,
        subscription_tier="free",     # they dropped to free after trial
        trial_started_at=_utc_now() - timedelta(days=31),
        trial_ends_at=_utc_now() - timedelta(days=1),
    )
    out = _call(user)
    assert out is user
    # The dep MUST NOT have touched totp_enabled.
    assert user.totp_enabled is False
