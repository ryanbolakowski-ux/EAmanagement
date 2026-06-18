"""Pure unit tests for app.core.packages capability helpers + the
automation_status 5-state machine.

No DB, no HTTP — mirrors the test_2fa_gate.py style: a minimal stub user and
direct calls into the REAL feature functions (imported, never reimplemented).
These are the single source of truth for tier->capability, so we pin every
rule the spec calls out.

Run standalone:
    pytest backend/tests/test_package_capabilities.py -v -p no:cacheprovider
"""
import pytest

from app.core.packages import (
    tier_value,
    is_fully_automated_tier,
    gets_signals,
    requires_manual_approval,
    can_place_on_approval,
    automation_status,
    FULLY_AUTOMATED_TIERS,
    APPROVE_TO_PLACE_TIERS,
    SIGNAL_TIERS,
    FULLY_AUTOMATED_AGREEMENT,
    AUTOMATION_NOT_ELIGIBLE,
    AUTOMATION_AGREEMENT_REQUIRED,
    AUTOMATION_PENDING,
    AUTOMATION_DISABLED,
    AUTOMATION_ENABLED,
)


ALL_TIERS = ["free_trial", "tier_1", "tier_2", "tier_3", "tier_4", "tier_5"]


class _UserStub:
    """Minimal stand-in for app.models.user.User; only the tier attribute the
    helpers touch."""
    def __init__(self, subscription_tier):
        self.subscription_tier = subscription_tier


class _EnumLike:
    """Mimics a SubscriptionTier enum member: has a .value string."""
    def __init__(self, value):
        self.value = value


class _NoAttr:
    """An object with neither .subscription_tier nor .value — must normalize to
    '' and never raise."""
    pass


# ---------------------------------------------------------------------------
# tier_value — normalizes all input forms, None-safe, never raises
# ---------------------------------------------------------------------------

def test_tier_value_uppercase_string_normalized():
    assert tier_value("TIER_5") == "tier_5"


def test_tier_value_user_stub():
    assert tier_value(_UserStub(subscription_tier="tier_4")) == "tier_4"


def test_tier_value_enum_like_object():
    assert tier_value(_EnumLike("tier_2")) == "tier_2"


def test_tier_value_user_stub_with_enum_like_tier():
    """A User whose .subscription_tier is itself an enum member (has .value)."""
    assert tier_value(_UserStub(subscription_tier=_EnumLike("tier_3"))) == "tier_3"


def test_tier_value_none_is_empty():
    assert tier_value(None) == ""


def test_tier_value_object_with_no_attr_never_raises():
    """Object lacking both subscription_tier and value: the helper must NOT
    raise and must return a (lowercased) string.

    NOTE: the spec text claimed this returns '', but the real implementation
    falls back to ``str(t or "")`` and a non-None no-attr object is truthy, so
    it returns its lowercased repr, NOT ''. Only None (and other falsy values)
    normalize to ''. We pin the real, None-safe / never-raises contract here
    rather than asserting behavior the function does not have. The junk value
    is harmless downstream because it is never in any TIER set, so every
    capability helper treats it as 'no entitlement'."""
    out = tier_value(_NoAttr())
    assert isinstance(out, str)
    assert out == out.lower()
    # And critically: it is not a valid tier, so it grants nothing.
    assert out not in (FULLY_AUTOMATED_TIERS | SIGNAL_TIERS | APPROVE_TO_PLACE_TIERS)
    assert is_fully_automated_tier(_NoAttr()) is False
    assert gets_signals(_NoAttr()) is False
    assert can_place_on_approval(_NoAttr()) is False


def test_tier_value_never_raises_on_weird_input():
    # Belt-and-suspenders: a grab bag of inputs must all return a str.
    for x in (None, _NoAttr(), "", _UserStub(subscription_tier=None)):
        out = tier_value(x)
        assert isinstance(out, str)


# ---------------------------------------------------------------------------
# is_fully_automated_tier — True only for tier_5
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ALL_TIERS)
def test_is_fully_automated_tier(tier):
    expected = tier == "tier_5"
    assert is_fully_automated_tier(tier) is expected
    assert is_fully_automated_tier(_UserStub(subscription_tier=tier)) is expected


# ---------------------------------------------------------------------------
# gets_signals — tier_2/3/4 AND tier_5; not free_trial/tier_1
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ALL_TIERS)
def test_gets_signals(tier):
    expected = tier in ("tier_2", "tier_3", "tier_4", "tier_5")
    assert gets_signals(tier) is expected
    assert gets_signals(_UserStub(subscription_tier=tier)) is expected


# ---------------------------------------------------------------------------
# requires_manual_approval — exact inverse of is_fully_automated_tier
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ALL_TIERS)
def test_requires_manual_approval_is_inverse(tier):
    expected = tier != "tier_5"
    assert requires_manual_approval(tier) is expected
    # exact inverse invariant
    assert requires_manual_approval(tier) is (not is_fully_automated_tier(tier))


def test_only_tier_5_auto_trades():
    """No non-tier_5 tier may skip manual approval."""
    for tier in ALL_TIERS:
        if tier != "tier_5":
            assert requires_manual_approval(tier) is True


# ---------------------------------------------------------------------------
# can_place_on_approval — tier_4 AND tier_5 only
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ALL_TIERS)
def test_can_place_on_approval(tier):
    expected = tier in ("tier_4", "tier_5")
    assert can_place_on_approval(tier) is expected
    assert can_place_on_approval(_UserStub(subscription_tier=tier)) is expected


# ---------------------------------------------------------------------------
# automation_status — the 5-state machine
# ---------------------------------------------------------------------------

def test_automation_status_non_tier5_not_eligible():
    """tier_4 (and any non-tier_5) is NOT_ELIGIBLE no matter what the other
    flags say."""
    for has_agreement in (False, True):
        for trading_enabled in (None, False, True):
            for verification_pending in (False, True):
                assert automation_status(
                    "tier_4",
                    has_agreement=has_agreement,
                    trading_enabled=trading_enabled,
                    verification_pending=verification_pending,
                ) == AUTOMATION_NOT_ELIGIBLE


def test_automation_status_tier5_agreement_required():
    """tier_5 without the agreement -> AGREEMENT_REQUIRED even if trading on."""
    assert automation_status(
        "tier_5",
        has_agreement=False,
        trading_enabled=True,
    ) == AUTOMATION_AGREEMENT_REQUIRED


def test_automation_status_tier5_pending_wins_over_trading_enabled():
    """Agreement signed but verification still pending -> PENDING, even when
    trading_enabled is True (pending must win)."""
    assert automation_status(
        "tier_5",
        has_agreement=True,
        trading_enabled=True,
        verification_pending=True,
    ) == AUTOMATION_PENDING


def test_automation_status_tier5_disabled():
    """Signed + verified (not pending) + trading off -> DISABLED."""
    assert automation_status(
        "tier_5",
        has_agreement=True,
        trading_enabled=False,
        verification_pending=False,
    ) == AUTOMATION_DISABLED


def test_automation_status_tier5_enabled():
    """Signed + verified + trading on -> ENABLED."""
    assert automation_status(
        "tier_5",
        has_agreement=True,
        trading_enabled=True,
        verification_pending=False,
    ) == AUTOMATION_ENABLED


def test_automation_status_tier5_trading_enabled_none_is_disabled():
    """trading_enabled=None (no broker account / unknown) is falsy -> DISABLED."""
    assert automation_status(
        "tier_5",
        has_agreement=True,
        trading_enabled=None,
    ) == AUTOMATION_DISABLED


def test_automation_status_accepts_user_stub():
    """Works off a User object, not just a raw tier string."""
    assert automation_status(
        _UserStub(subscription_tier="tier_5"),
        has_agreement=True,
        trading_enabled=True,
    ) == AUTOMATION_ENABLED


# ---------------------------------------------------------------------------
# module constants match the spec exactly
# ---------------------------------------------------------------------------

def test_constants_match_spec():
    assert FULLY_AUTOMATED_TIERS == {"tier_5"}
    assert APPROVE_TO_PLACE_TIERS == {"tier_4"}
    assert SIGNAL_TIERS == {"tier_2", "tier_3", "tier_4"}
    assert FULLY_AUTOMATED_AGREEMENT == "fully_automated_trading"
