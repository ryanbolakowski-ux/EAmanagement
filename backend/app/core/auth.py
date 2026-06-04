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
    if _user_needs_2fa(current_user):
        _raise_2fa_required()
    return current_user
