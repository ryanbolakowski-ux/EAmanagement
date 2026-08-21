from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import JWTError

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.core.security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
        user_id: str = payload.get("sub")
        if not user_id:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise credentials_exception
    return user


# 2FA gate helpers
# Mandatory 2FA for paid + trial users. Raises 403 with structured detail
# {"code": "requires_2fa_setup", ...} when the user is on a billable tier
# (or active trial) and has not enabled TOTP.
#
# Frontends should intercept this 403 and redirect the user to /settings/2fa.
# Free users WITHOUT an active trial are exempt (2FA stays optional).
# Once a subscription/trial ends the gate opens; existing totp_enabled config
# is preserved (we never mutate user 2FA state from this dependency).
def _user_needs_2fa(current_user: User) -> bool:
    """Return True when the user is paid/trial AND totp_enabled is False."""
    if current_user.totp_enabled:
        return False
    now = datetime.now(timezone.utc)
    tier = (current_user.subscription_tier or "").strip().lower()
    # Empty / 'free' = free user with no subscription; gate stays open.
    is_paid_tier = tier not in ("", "free", "free_trial")

    trial_started = getattr(current_user, "trial_started_at", None)
    trial_ends = getattr(current_user, "trial_ends_at", None)
    is_active_trial = (
        trial_started is not None
        and (trial_ends is None or trial_ends > now)
    )
    return is_paid_tier or is_active_trial


def _raise_2fa_required() -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "requires_2fa_setup",
            "message": (
                "Two-factor authentication is required for paid and trial "
                "accounts. Set up 2FA at /settings/2fa to continue."
            ),
            "setup_url": "/settings/2fa",
        },
    )


# Trial-expiry gate helpers
# The tier gates below check ONLY subscription_tier; nothing blocks a
# free-trial user once trial_ends_at passes, so expired trials kept full
# access indefinitely. This helper closes that hole WITHOUT mutating the row
# (no downgrade write in the request path).
#
# The ONLY trial tier is SubscriptionTier.FREE_TRIAL ("free_trial"); every
# other value (tier_2..tier_5, tier_1 legacy alias) is a paid/non-trial tier
# and is NEVER affected here. trial_ends_at is DateTime(timezone=True) (stored
# UTC). Fail-OPEN by design: a NULL trial_ends_at on a trial user is treated
# as NON-expired (legacy comps predate the column), and any non-trial tier
# short-circuits to False before we ever look at the date.
def _trial_expired(current_user: User) -> bool:
    """Return True only when the user is on the free-trial tier AND
    trial_ends_at is set AND already in the past (UTC-correct)."""
    tier = (current_user.subscription_tier or "").strip().lower()
    if tier != SubscriptionTier.FREE_TRIAL.value:
        return False  # paid / non-trial tiers can never expire here
    trial_ends = getattr(current_user, "trial_ends_at", None)
    if trial_ends is None:
        return False  # fail-open: legacy trial rows with no end date
    now = datetime.now(timezone.utc)
    # Column is tz-aware (stored UTC); guard any naive legacy value by
    # interpreting it as UTC so the comparison can never raise.
    if trial_ends.tzinfo is None:
        trial_ends = trial_ends.replace(tzinfo=timezone.utc)
    return trial_ends < now


def _raise_trial_expired() -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={
            "code": "trial_expired",
            "message": (
                "Your free trial has ended. Upgrade to a paid plan to "
                "continue using this feature."
            ),
            "upgrade_url": "/pricing",
        },
    )


async def require_2fa_when_paid(
    current_user: User = Depends(get_current_user),
) -> User:
    if _user_needs_2fa(current_user):
        _raise_2fa_required()
    return current_user


def require_tier(*tiers: SubscriptionTier):
    """Dependency factory: require user to be on one of the given tiers.

    Also enforces the 2FA gate — every tier-restricted route is by definition
    a paid/trial feature, so 2FA must be set up first.
    """
    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.subscription_tier not in tiers:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This feature requires one of: {[t.value for t in tiers]}",
            )
        # Trial-expiry gate: an expired free trial loses feature access and is
        # routed to /pricing (checked before 2FA so we don't send an expired
        # trial user to set up 2FA they no longer need). Paid tiers unaffected.
        if _trial_expired(current_user):
            _raise_trial_expired()
        if _user_needs_2fa(current_user):
            _raise_2fa_required()
        return current_user
    return checker


def require_live_trading(current_user: User = Depends(get_current_user)) -> User:
    live_tiers = {SubscriptionTier.TIER_3, SubscriptionTier.TIER_4, SubscriptionTier.TIER_5}
    if current_user.subscription_tier not in live_tiers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live trading requires a paid plan.",
        )
    # Defense-in-depth: free_trial is not a live tier so this is a no-op today,
    # but keeps the trial-expiry gate uniform if live_tiers ever changes.
    if _trial_expired(current_user):
        _raise_trial_expired()
    if _user_needs_2fa(current_user):
        _raise_2fa_required()
    return current_user
