"""Saro STOCK-pick self-serve activation — eligibility + backward-compat gate.

Pure unit tests: no DB, no network, no email. They pin the tier-eligibility
rules and the recipient-gate SQL invariants that keep activation ADDITIVE and
backward-compatible (existing recipients must never be dropped, new tier_3+
users must NOT be auto-activated).
"""
from app.core.packages import gets_saro_stock, SARO_STOCK_TIERS, tier_value
from app.core.saro import SARO_RECIPIENT_WHERE


class _FakeEnum:
    """Mimics a SubscriptionTier enum (has .value)."""
    def __init__(self, v):
        self.value = v


class _FakeUser:
    def __init__(self, tier):
        self.subscription_tier = tier


def test_saro_eligible_tiers():
    for t in ("tier_3", "tier_4", "tier_5"):
        assert gets_saro_stock(t) is True, t


def test_saro_ineligible_tiers():
    # tier_2 is FUTURES signals — a common footgun (it's in SIGNAL_TIERS) but is
    # NOT eligible for the Saro STOCK pick.
    for t in ("free_trial", "tier_1", "tier_2", "", None, "bogus"):
        assert gets_saro_stock(t) is False, t


def test_saro_tiers_constant():
    assert SARO_STOCK_TIERS == {"tier_3", "tier_4", "tier_5"}
    assert "tier_2" not in SARO_STOCK_TIERS   # futures, not stock
    assert "tier_5" in SARO_STOCK_TIERS       # fully-automated still gets the pick


def test_gets_saro_stock_accepts_user_and_enum_and_case():
    assert gets_saro_stock(_FakeUser("tier_3")) is True
    assert gets_saro_stock(_FakeUser(_FakeEnum("tier_5"))) is True
    assert gets_saro_stock("TIER_4") is True          # case-insensitive
    assert gets_saro_stock(_FakeUser("tier_2")) is False
    assert tier_value(_FakeUser(_FakeEnum("TIER_3"))) == "tier_3"


def test_recipient_gate_is_backward_compatible():
    w = SARO_RECIPIENT_WHERE.lower()
    # Absent/NULL flag must default to NOT activated (additive opt-in) — a new
    # tier_3+ user is NOT emailed until they self-activate.
    assert "coalesce(u.saro_signals_enabled, false) = true" in w
    # Only Saro-eligible tiers, and only active accounts.
    assert "lower(u.subscription_tier) in ('tier_3', 'tier_4', 'tier_5')" in w
    assert "u.is_active = true" in w
