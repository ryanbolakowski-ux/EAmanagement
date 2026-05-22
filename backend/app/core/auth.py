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


def require_tier(*tiers: SubscriptionTier):
    """Dependency factory: require user to be on one of the given tiers."""
    async def checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.subscription_tier not in tiers:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This feature requires one of: {[t.value for t in tiers]}",
            )
        return current_user
    return checker


def require_live_trading(current_user: User = Depends(get_current_user)) -> User:
    live_tiers = {SubscriptionTier.TIER_3, SubscriptionTier.TIER_4, SubscriptionTier.TIER_5}
    if current_user.subscription_tier not in live_tiers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Live trading requires a paid plan.",
        )
    return current_user
