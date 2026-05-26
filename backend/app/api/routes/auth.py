import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

import pyotp
import redis as redis_lib
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from loguru import logger
from app.core.auth import get_current_user
from app.core.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)
from app.database import get_db
from app.models.user import SubscriptionTier, User
from app.services import email as email_service

router = APIRouter()

# Redis is used to hold short-lived tokens (password reset, 2FA challenge).
# Keeping these out of the DB avoids a migration and gives us TTL for free.
_redis = redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True, db=0)
PWRESET_TTL = 60 * 60          # 1 hour
TWOFA_CHALLENGE_TTL = 5 * 60   # 5 minutes


class RegisterRequest(BaseModel):
    email: EmailStr
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    subscription_tier: str


class LoginResponse(BaseModel):
    """Login can either return tokens (no 2FA) or a 2FA challenge."""
    requires_2fa: bool = False
    challenge_token: Optional[str] = None
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    user_id: Optional[str] = None
    email: Optional[str] = None
    subscription_tier: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    email: str
    username: str
    subscription_tier: str
    is_active: bool
    trial_ends_at: datetime | None = None
    is_admin: bool = False
    totp_enabled: bool = False
    totp_setup_pending: bool = False  # secret exists but user hasn't confirmed code
    kyc_status: str = 'not_started'
    kyc_verified_at: datetime | None = None
    country_code: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class Verify2FARequest(BaseModel):
    challenge_token: str
    code: str


class Setup2FAResponse(BaseModel):
    secret: str
    otpauth_url: str


class Confirm2FARequest(BaseModel):
    code: str


def _build_tokens(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token({"sub": str(user.id)}),
        refresh_token=create_refresh_token({"sub": str(user.id)}),
        user_id=str(user.id),
        email=user.email,
        subscription_tier=user.subscription_tier,
    )


# ── Register ────────────────────────────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    data: RegisterRequest,
    background: BackgroundTasks,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(User).where(User.email == data.email))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Email already registered.")

    trial_end = datetime.utcnow() + timedelta(days=30)
    user = User(
        email=data.email,
        username=data.username,
        hashed_password=hash_password(data.password),
        subscription_tier="free_trial",
        trial_started_at=datetime.utcnow(),
        trial_ends_at=trial_end,
    )
    db.add(user)
    await db.flush()

    # Seed the canonical strategy set so every new account starts with the
    # same library — no template picker UI needed; users edit/delete from here.
    try:
        from app.scripts.seed_strategies import seed_user_strategies
        await seed_user_strategies(db, user.id)
    except Exception as e:
        logger.warning(f'[auth.register] strategy seed failed: {e}')

    # Welcome the new user
    background.add_task(email_service.send_welcome_email, user.email, user.username)

    # Notify the platform owner (theta.algos@yahoo.com) — every signup
    # triggers a one-line summary email so unusual bursts are spotted fast.
    signup_ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
    signup_country = ""
    try:
        from app.middleware.geo_block import _lookup_country
        signup_country = await _lookup_country(signup_ip) or ""
    except Exception:
        pass
    background.add_task(email_service.send_admin_new_user_notification,
                       user.email, user.username, signup_ip, signup_country)

    tokens = _build_tokens(user)
    return tokens


# ── Login (with optional 2FA challenge) ─────────────────────────────────────

# Redis-backed rate limiter — 5 failed logins per IP per minute. Catches
# credential stuffing without breaking real users (5 wrong passwords in 60s
# is already extreme for a legitimate user). We use Redis INCR with EX so
# it survives backend restarts and works across multi-worker deploys.
def _enforce_login_rate_limit(request: Request):
    try:
        ip = request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "0.0.0.0")
        key = f"ratelimit:login:{ip}:{int(__import__('time').time() // 60)}"
        count = _redis.incr(key)
        if count == 1:
            _redis.expire(key, 90)
        if count > 5:
            raise HTTPException(status_code=429, detail="Too many login attempts. Wait 1 minute.")
    except HTTPException:
        raise
    except Exception:
        pass  # fail-open: never block real users on a Redis hiccup

@router.post("/login", response_model=LoginResponse)
async def login(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    _enforce_login_rate_limit(request)
    result = await db.execute(select(User).where(func.lower(User.email) == form.username.lower().strip()))
    user = result.scalar_one_or_none()
    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials.")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled.")

    if user.totp_enabled and user.totp_secret:
        challenge = secrets.token_urlsafe(32)
        _redis.setex(f"2fa_pending:{challenge}", TWOFA_CHALLENGE_TTL, str(user.id))
        return LoginResponse(requires_2fa=True, challenge_token=challenge)

    tokens = _build_tokens(user)
    return LoginResponse(
        requires_2fa=False,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        user_id=tokens.user_id,
        email=tokens.email,
        subscription_tier=tokens.subscription_tier,
    )


@router.post("/verify-2fa", response_model=TokenResponse)
async def verify_2fa(data: Verify2FARequest, db: AsyncSession = Depends(get_db)):
    user_id = _redis.get(f"2fa_pending:{data.challenge_token}")
    if not user_id:
        raise HTTPException(status_code=400, detail="2FA challenge expired or invalid. Please log in again.")

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or not user.totp_secret:
        raise HTTPException(status_code=400, detail="Invalid 2FA state.")

    if not pyotp.TOTP(user.totp_secret).verify(data.code, valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid authentication code.")

    _redis.delete(f"2fa_pending:{data.challenge_token}")
    return _build_tokens(user)


# ── Me ──────────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    return UserResponse(
        id=str(current_user.id),
        email=current_user.email,
        username=current_user.username,
        subscription_tier=current_user.subscription_tier,
        is_active=current_user.is_active,
        trial_ends_at=current_user.trial_ends_at,
        is_admin=bool(current_user.is_admin),
        totp_enabled=bool(current_user.totp_enabled),
        totp_setup_pending=(not bool(current_user.totp_enabled)
                            and bool(current_user.totp_secret)),
        kyc_status=getattr(current_user, 'kyc_status', None) or 'not_started',
        kyc_verified_at=getattr(current_user, 'kyc_verified_at', None),
        country_code=getattr(current_user, 'country_code', None),
    )


# ── Forgot / reset password ─────────────────────────────────────────────────

@router.post("/forgot-password")
async def forgot_password(
    data: ForgotPasswordRequest,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    """Always returns 200 to avoid leaking which emails are registered."""
    user = (await db.execute(select(User).where(User.email == data.email))).scalar_one_or_none()
    if user and user.is_active:
        token = secrets.token_urlsafe(32)
        _redis.setex(f"pwreset:{token}", PWRESET_TTL, str(user.id))
        background.add_task(email_service.send_password_reset_email, user.email, user.username, token)
    return {"status": "ok", "detail": "If that email is registered, a reset link is on its way."}


@router.post("/reset-password")
async def reset_password(data: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    if len(data.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    key = f"pwreset:{data.token}"
    user_id = _redis.get(key)
    if not user_id:
        raise HTTPException(status_code=400, detail="Reset link is invalid or has expired.")

    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Reset link is invalid or has expired.")

    user.hashed_password = hash_password(data.new_password)
    _redis.delete(key)
    await db.commit()
    return {"status": "ok"}


# ── 2FA setup / confirm / disable ───────────────────────────────────────────

@router.post("/2fa/setup", response_model=Setup2FAResponse)
async def setup_2fa(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate a TOTP secret and return the otpauth URL for the user to scan.

    Stores the secret on the user but leaves totp_enabled=False until they
    confirm with /2fa/confirm.
    """
    if current_user.totp_enabled:
        raise HTTPException(status_code=400, detail="2FA is already enabled. Disable it first to re-enroll.")

    secret = pyotp.random_base32()
    current_user.totp_secret = secret
    await db.commit()

    otpauth_url = pyotp.totp.TOTP(secret).provisioning_uri(
        name=current_user.email,
        issuer_name="Theta Algos",
    )
    return Setup2FAResponse(secret=secret, otpauth_url=otpauth_url)


@router.post("/2fa/confirm")
async def confirm_2fa(
    data: Confirm2FARequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.totp_secret:
        raise HTTPException(status_code=400, detail="No pending 2FA setup. Call /2fa/setup first.")
    if not pyotp.TOTP(current_user.totp_secret).verify(data.code, valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid authentication code.")

    current_user.totp_enabled = True
    await db.commit()
    return {"status": "ok", "totp_enabled": True}


@router.post("/2fa/disable")
async def disable_2fa(
    data: Confirm2FARequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not current_user.totp_enabled or not current_user.totp_secret:
        raise HTTPException(status_code=400, detail="2FA is not enabled.")
    if not pyotp.TOTP(current_user.totp_secret).verify(data.code, valid_window=1):
        raise HTTPException(status_code=401, detail="Invalid authentication code.")

    current_user.totp_secret = None
    current_user.totp_enabled = False
    await db.commit()
    return {"status": "ok", "totp_enabled": False}


# Diagnostic: tells you if a login attempt would succeed without actually issuing a JWT.
# Use this when 'invalid credentials' is mysterious — quickly proves whether backend rejects you
# vs the frontend never reaching the backend.
@router.post('/diag-login')
async def diag_login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """Diagnostic: same logic as /login but returns reason + masked info. Never issues a token."""
    typed_email = (form.username or '').strip()
    result = await db.execute(select(User).where(func.lower(User.email) == typed_email.lower()))
    user = result.scalar_one_or_none()
    if not user:
        return {'ok': False, 'reason': 'NO_USER_FOUND', 'hint': f'no user with email matching (case-insensitive): {typed_email}'}
    if not verify_password(form.password, user.hashed_password):
        return {'ok': False, 'reason': 'WRONG_PASSWORD',
                'hint': 'email matched a user but the password does not — your password manager may be filling an old value',
                'user_email': user.email, 'is_active': user.is_active}
    if not user.is_active:
        return {'ok': False, 'reason': 'ACCOUNT_DISABLED', 'user_email': user.email}
    return {'ok': True, 'reason': 'WOULD_SUCCEED', 'user_email': user.email,
            'requires_2fa': bool(user.totp_enabled and user.totp_secret)}
