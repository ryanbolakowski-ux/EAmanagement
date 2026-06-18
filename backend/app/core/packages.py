"""Package/tier capability helpers — the SINGLE source of truth for what each
subscription tier is allowed to do. The automation/approval guards, the signal
fan-out, and the UI all read from here so the rules can't drift apart.

Tiers (see app/models/user.py SubscriptionTier):
  free_trial : 30-day trial — scanner preview, paper only
  tier_1     : legacy/base
  tier_2     : $49  — futures signals (email)
  tier_3     : $99  — options scanner morning email
  tier_4     : $199 — options live via Tradier (MANUAL CONFIRM = approve/decline)
  tier_5     : $399 — FULLY AUTOMATED, no clicks
"""
from typing import Optional

# The ONLY tier that may run unattended automated trading.
FULLY_AUTOMATED_TIERS = {"tier_5"}
# Tiers that receive trade-idea signals (and therefore the approve/decline UX).
SIGNAL_TIERS = {"tier_2", "tier_3", "tier_4"}
# Tiers permitted to PLACE a live trade after the user approves a signal
# (they have a real brokerage connection / live-execution entitlement).
APPROVE_TO_PLACE_TIERS = {"tier_4"}

# Agreement kinds (must match app/api/routes/legal.py CURRENT_VERSIONS).
FULLY_AUTOMATED_AGREEMENT = "fully_automated_trading"
SIGNALS_AGREEMENT = "signals_disclosure"


def tier_value(user_or_tier) -> str:
    """Normalize a User, a SubscriptionTier enum, or a raw string to the bare
    tier string (e.g. 'tier_5'). Accepts None safely."""
    t = getattr(user_or_tier, "subscription_tier", user_or_tier)
    return (t.value if hasattr(t, "value") else str(t or "")).lower()


def is_fully_automated_tier(user_or_tier) -> bool:
    """True only for the fully-automated package (tier_5)."""
    return tier_value(user_or_tier) in FULLY_AUTOMATED_TIERS


def gets_signals(user_or_tier) -> bool:
    """True if the user receives trade-idea signals (signal tiers + fully-auto,
    which still gets notified)."""
    return tier_value(user_or_tier) in (SIGNAL_TIERS | FULLY_AUTOMATED_TIERS)


def requires_manual_approval(user_or_tier) -> bool:
    """True if the user must approve/decline each trade idea — i.e. anyone who
    is NOT on the fully-automated package. These users never get auto live
    trades; the engine routes their signals to the approve/decline flow."""
    return not is_fully_automated_tier(user_or_tier)


def can_place_on_approval(user_or_tier) -> bool:
    """True if, AFTER the user approves a trade idea, the platform is allowed to
    place it for them (broker/live-execution tiers). Final placement is still
    gated at call time by broker connection, permissions, risk, and required
    confirmations — this only says the tier is eligible."""
    return tier_value(user_or_tier) in (APPROVE_TO_PLACE_TIERS | FULLY_AUTOMATED_TIERS)


# Automation-status UI states (the user-facing labels in the spec).
AUTOMATION_NOT_ELIGIBLE = "not_eligible"      # not the fully-automated package
AUTOMATION_AGREEMENT_REQUIRED = "agreement_required"
AUTOMATION_PENDING = "pending"                # agreement signed, email-code not yet verified
AUTOMATION_DISABLED = "disabled"              # eligible + signed, automation off
AUTOMATION_ENABLED = "enabled"                # eligible + signed + verified + trading on


def automation_status(
    user_or_tier,
    *,
    has_agreement: bool,
    trading_enabled: Optional[bool] = None,
    verification_pending: bool = False,
) -> str:
    """Resolve the user's automation access state for the UI/badge.
    Order: not-eligible -> agreement-required -> pending -> enabled/disabled."""
    if not is_fully_automated_tier(user_or_tier):
        return AUTOMATION_NOT_ELIGIBLE
    if not has_agreement:
        return AUTOMATION_AGREEMENT_REQUIRED
    if verification_pending:
        return AUTOMATION_PENDING
    return AUTOMATION_ENABLED if trading_enabled else AUTOMATION_DISABLED
