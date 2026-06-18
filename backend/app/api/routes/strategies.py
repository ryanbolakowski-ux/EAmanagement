import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from pydantic import BaseModel
from typing import Optional

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.models.strategy import Strategy, StrategyStatus
from app.core.auth import require_tier, get_current_user, require_2fa_when_paid
from app.engines.strategy_classification import classify_asset_class

router = APIRouter()
# 2FA gate: POST/PUT/DELETE routes here require totp_enabled if user is on paid/trial

require_paid_user = require_tier(
    SubscriptionTier.FREE_TRIAL,
    SubscriptionTier.TIER_2,
    SubscriptionTier.TIER_3,
    SubscriptionTier.TIER_4,
    SubscriptionTier.TIER_5,
)


# 2FA-gated paid-user dep. Used on write routes (POST/PUT/DELETE/PATCH).
# Composes tier check + 2FA check: tier check first (faster); 2FA check
# raises 403 with detail.code='requires_2fa_setup' for paid/trial users
# who haven't enrolled TOTP yet.
async def require_paid_user_with_2fa(
    user: User = Depends(require_paid_user),
    _gated: User = Depends(require_2fa_when_paid),
) -> User:
    return user


class StrategyCreate(BaseModel):
    name: str
    description: Optional[str] = None
    # User-created strategies default to ACTIVE so they immediately show up in
    # dropdowns / optimization / paper / signals. A client may still submit
    # "draft" / "paused" / "archived" explicitly and it is respected.
    status: str = "active"
    instruments: list[str] = ["ES"]
    primary_timeframe: str = "15m"
    execution_timeframe: str = "1m"
    higher_timeframes: list[str] = []
    risk_reward_ratio: float = 2.0
    stop_loss_type: str = "structure"
    stop_loss_ticks: Optional[int] = None
    max_contracts: int = 1
    session_filters: list[str] = []
    fvg_min_size_ticks: int = 4
    fvg_max_size_ticks: Optional[int] = None
    max_daily_loss: Optional[float] = None
    max_trades_per_day: Optional[int] = None
    rule_tree: dict = {}


class StrategyResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    status: str
    instruments: list
    primary_timeframe: str
    execution_timeframe: str
    higher_timeframes: list = []
    risk_reward_ratio: float
    stop_loss_type: str
    session_filters: list
    starred: bool = False
    # Derived in code from `instruments` (the DB has no asset_class
    # column). 'futures' | 'options' | 'stock' | 'unknown'.
    asset_class: str = "unknown"
    created_at: str
    # Engine selector (V1 generic vs V2 dedicated setup). engine_version is
    # read from rule_tree; v2_available is True only when a dedicated setup
    # exists for this strategy name.
    rule_tree: dict = {}
    engine_version: str = "v1"
    v2_available: bool = False

    class Config:
        from_attributes = True


@router.get("/", response_model=list[StrategyResponse])
async def list_strategies(
    current_user: User = Depends(require_paid_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.user_id == current_user.id)
    )
    return [
        StrategyResponse(
            id=str(s.id), name=s.name, description=s.description,
            status=s.status.value, instruments=s.instruments,
            primary_timeframe=s.primary_timeframe, execution_timeframe=s.execution_timeframe,
            higher_timeframes=s.higher_timeframes or [],
            risk_reward_ratio=s.risk_reward_ratio, stop_loss_type=s.stop_loss_type,
            session_filters=s.session_filters, starred=getattr(s, "starred", False),
            asset_class=classify_asset_class(s.instruments), **_engine_meta(s),
            created_at=s.created_at.isoformat(),
        )
        for s in result.scalars().all()
    ]



# ── NAME-MODERATION-V1: block offensive strategy names (metadata hygiene) ──
_NM_LEET = {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
            "@": "a", "$": "s", "!": "i"}
#: Always-block substrings (chosen to avoid common-word collisions).
_NM_HARD = ("nigg", "niglet", "faggot", "kike", "wetback", "beaner",
            "tranny", "retard", "rapist")
#: Block only as a WHOLE normalized token (avoids "suspicious"->spic, "grape"->rape).
_NM_EXACT = {"fag", "fags", "spic", "spics", "chink", "coon", "coons", "gook",
             "paki", "kkk", "rape", "cunt", "cunts", "whore", "slut", "jap", "nig"}


def _nm_normalize(tok: str) -> str:
    # Substitute leet BEFORE dropping non-alnum, else "@/$/!" are removed before
    # they can map to a/s/i (so "f@g" must normalize to "fag", not "fg").
    tok = "".join(_NM_LEET.get(c, c) for c in (tok or "").lower())
    return "".join(c for c in tok if c.isalnum())


def is_offensive_name(name: str) -> bool:
    """True if `name` contains a slur / strong profanity (leet-normalized)."""
    import re as _re
    raw = (name or "").lower()
    toks = [_nm_normalize(t) for t in _re.split(r"[^a-z0-9@$!]+", raw) if t]
    for t in toks:
        if not t:
            continue
        if t in _NM_EXACT:
            return True
        if any(h in t for h in _NM_HARD):
            return True
    return False


def _reject_offensive_name(name: str) -> None:
    if is_offensive_name(name):
        raise HTTPException(
            status_code=400,
            detail="That strategy name isn't allowed. Please choose a name without "
                   "profanity or slurs — names appear in logs, emails and exports.",
        )


@router.post("/", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    data: StrategyCreate,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    payload = data.model_dump()
    _reject_offensive_name(payload.get("name"))  # NAME-MODERATION-V1
    _status_raw = (payload.pop("status", None) or "active")
    try:
        _status_enum = StrategyStatus(_status_raw)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid status {_status_raw!r}. Must be one of: "
                   + ", ".join(s.value for s in StrategyStatus),
        )
    strategy = Strategy(
        user_id=current_user.id,
        status=_status_enum,
        **payload,
    )
    db.add(strategy)
    await db.flush()
    return StrategyResponse(
        id=str(strategy.id), name=strategy.name, description=strategy.description,
        status=strategy.status.value, instruments=strategy.instruments,
        primary_timeframe=strategy.primary_timeframe, execution_timeframe=strategy.execution_timeframe,
        higher_timeframes=strategy.higher_timeframes or [],
        risk_reward_ratio=strategy.risk_reward_ratio, stop_loss_type=strategy.stop_loss_type,
        session_filters=strategy.session_filters, starred=getattr(strategy, "starred", False),
        asset_class=classify_asset_class(strategy.instruments), **_engine_meta(strategy),
        created_at=strategy.created_at.isoformat(),
    )


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(
    strategy_id: str,
    current_user: User = Depends(require_paid_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    return StrategyResponse(
        id=str(strategy.id), name=strategy.name, description=strategy.description,
        status=strategy.status.value, instruments=strategy.instruments,
        primary_timeframe=strategy.primary_timeframe, execution_timeframe=strategy.execution_timeframe,
        higher_timeframes=strategy.higher_timeframes or [],
        risk_reward_ratio=strategy.risk_reward_ratio, stop_loss_type=strategy.stop_loss_type,
        session_filters=strategy.session_filters, starred=getattr(strategy, "starred", False),
        asset_class=classify_asset_class(strategy.instruments), **_engine_meta(strategy),
        created_at=strategy.created_at.isoformat(),
    )


@router.put("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: str,
    data: StrategyCreate,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    payload = data.model_dump()
    _reject_offensive_name(payload.get("name"))  # NAME-MODERATION-V1
    _status_raw = payload.pop("status", None)
    for key, value in payload.items():
        setattr(strategy, key, value)
    if _status_raw is not None:
        try:
            strategy.status = StrategyStatus(_status_raw)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid status {_status_raw!r}. Must be one of: "
                       + ", ".join(s.value for s in StrategyStatus),
            )
    await db.flush()
    return StrategyResponse(
        id=str(strategy.id), name=strategy.name, description=strategy.description,
        status=strategy.status.value, instruments=strategy.instruments,
        primary_timeframe=strategy.primary_timeframe, execution_timeframe=strategy.execution_timeframe,
        higher_timeframes=strategy.higher_timeframes or [],
        risk_reward_ratio=strategy.risk_reward_ratio, stop_loss_type=strategy.stop_loss_type,
        session_filters=strategy.session_filters, starred=getattr(strategy, "starred", False),
        asset_class=classify_asset_class(strategy.instruments), **_engine_meta(strategy),
        created_at=strategy.created_at.isoformat(),
    )


@router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strategy(
    strategy_id: str,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # FK constraints are configured in Postgres with the right cascade rules:
    #   trades / sessions / backtests / optimizations  → ON DELETE SET NULL
    #     (history rows survive with strategy_id = NULL)
    #   strategy_conditions / signal_watchers / signals → ON DELETE CASCADE
    #     (these only make sense alongside their strategy)
    # So a plain raw DELETE is enough; the database handles the rest atomically.
    from sqlalchemy import text as _sql
    await db.execute(_sql("DELETE FROM strategies WHERE id = :sid"), {"sid": strategy_id})
    await db.commit()


def _engine_meta(s) -> dict:
    """Per-strategy engine fields: rule_tree, the chosen engine (v1/v2 from
    rule_tree), and whether a V2 dedicated setup actually exists for this name."""
    rt = getattr(s, "rule_tree", None) or {}
    try:
        from app.engines.ict import setups as _setups  # noqa: F401  (registers)
        from app.engines.ict.registry import get_setup
        v2 = get_setup(s.name, rt) is not None
    except Exception:
        v2 = False
    return {"rule_tree": rt,
            "engine_version": str((rt or {}).get("engine_version", "v1") or "v1").strip().lower(),
            "v2_available": bool(v2)}


def _strategy_to_response(s) -> "StrategyResponse":
    return StrategyResponse(
        id=str(s.id), name=s.name, description=s.description,
        status=s.status.value, instruments=s.instruments,
        primary_timeframe=s.primary_timeframe, execution_timeframe=s.execution_timeframe,
        higher_timeframes=s.higher_timeframes or [],
        risk_reward_ratio=s.risk_reward_ratio, stop_loss_type=s.stop_loss_type,
        session_filters=s.session_filters, starred=getattr(s, "starred", False),
        asset_class=classify_asset_class(s.instruments), **_engine_meta(s),
        created_at=s.created_at.isoformat(),
    )


async def _set_status(strategy_id, current_user, db, new_status: StrategyStatus):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    strategy.status = new_status
    await db.commit()
    await db.refresh(strategy)
    return _strategy_to_response(strategy)


@router.post("/{strategy_id}/activate", response_model=StrategyResponse)
async def activate_strategy(
    strategy_id: str,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    """Publish/activate a strategy. Explicit, unambiguous flow used by the UI's
    Activate button (the generic PUT also honors status, but this endpoint makes
    the intent and any failure obvious)."""
    return await _set_status(strategy_id, current_user, db, StrategyStatus.ACTIVE)


@router.post("/{strategy_id}/deactivate", response_model=StrategyResponse)
async def deactivate_strategy(
    strategy_id: str,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    """Move a strategy back to draft (unpublish)."""
    return await _set_status(strategy_id, current_user, db, StrategyStatus.DRAFT)


class StarToggle(BaseModel):
    starred: bool


@router.patch('/{strategy_id}/star', response_model=StrategyResponse)
async def toggle_strategy_star(
    strategy_id: str,
    data: StarToggle,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail='Strategy not found.')
    strategy.starred = bool(data.starred)
    await db.commit()
    await db.refresh(strategy)
    return StrategyResponse(
        id=str(strategy.id), name=strategy.name, description=strategy.description,
        status=strategy.status.value, instruments=strategy.instruments,
        primary_timeframe=strategy.primary_timeframe, execution_timeframe=strategy.execution_timeframe,
        higher_timeframes=strategy.higher_timeframes or [],
        risk_reward_ratio=strategy.risk_reward_ratio, stop_loss_type=strategy.stop_loss_type,
        session_filters=strategy.session_filters, starred=bool(strategy.starred),
        asset_class=classify_asset_class(strategy.instruments), **_engine_meta(strategy),
        created_at=strategy.created_at.isoformat(),
    )
"""Patch — append to strategies.py to add share/import endpoints.

Sharing flow:
  1. Owner POSTs to /strategies/:id/share → server returns a stable token
     (created lazily on first call; same token returned on subsequent calls
     until owner POSTs to /strategies/:id/share?regenerate=true).
  2. Owner shares the URL https://thetaalgos.com/app/strategies/shared/<token>.
  3. Recipient (authenticated) GETs /strategies/shared/<token>/preview to
     read the strategy details without importing.
  4. Recipient POSTs /strategies/shared/<token>/import to copy into their
     own account. The copy is a new row owned by the recipient — original
     stays under the sharer's account.
"""

import secrets
from pydantic import BaseModel


def _gen_share_token() -> str:
    # 16 chars of base64url — short enough to share, large enough to be
    # un-guessable (2^96 possible).
    return secrets.token_urlsafe(12)


class ShareTokenResponse(BaseModel):
    token: str
    share_url: str


@router.post("/{strategy_id}/share", response_model=ShareTokenResponse)
async def generate_share_token(
    strategy_id: str,
    regenerate: bool = False,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    if regenerate or not getattr(strategy, "share_token", None):
        # Generate a fresh token (and overwrite any existing). Tiny chance of
        # collision — re-try a couple of times if so.
        for _ in range(5):
            new_token = _gen_share_token()
            existing = await db.execute(
                text("SELECT 1 FROM strategies WHERE share_token = :t"), {"t": new_token}
            )
            if existing.fetchone() is None:
                break
        else:
            raise HTTPException(status_code=500, detail="Could not allocate a unique share token.")
        await db.execute(
            text("UPDATE strategies SET share_token = :t WHERE id = :sid"),
            {"t": new_token, "sid": strategy_id},
        )
        await db.commit()
        token = new_token
    else:
        token = strategy.share_token

    from app.config import settings
    return ShareTokenResponse(
        token=token,
        share_url=f"{settings.FRONTEND_URL}/app/strategies/shared/{token}",
    )


@router.delete("/{strategy_id}/share", status_code=204)
async def revoke_share_token(
    strategy_id: str,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    await db.execute(
        text("UPDATE strategies SET share_token = NULL WHERE id = :sid AND user_id = :uid"),
        {"sid": strategy_id, "uid": str(current_user.id)},
    )
    await db.commit()
    return None


class SharedStrategyPreview(BaseModel):
    name: str
    description: Optional[str]
    instruments: list
    primary_timeframe: str
    execution_timeframe: str
    higher_timeframes: list
    risk_reward_ratio: float
    stop_loss_type: str
    session_filters: list
    shared_by_username: Optional[str] = None


@router.get("/shared/{token}/preview", response_model=SharedStrategyPreview)
async def preview_shared_strategy(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """PUBLIC endpoint — anyone with the share link can preview, no auth required.
    The actual import below still requires auth (must have an account)."""
    row = await db.execute(text("""
        SELECT s.name, s.description, s.instruments, s.primary_timeframe, s.execution_timeframe,
               s.higher_timeframes, s.risk_reward_ratio, s.stop_loss_type, s.session_filters,
               u.username AS sharer
          FROM strategies s
          JOIN users u ON u.id = s.user_id
         WHERE s.share_token = :t
    """), {"t": token})
    r = row.fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="That share link is invalid or has been revoked.")
    return SharedStrategyPreview(
        name=r.name, description=r.description, instruments=r.instruments or [],
        primary_timeframe=r.primary_timeframe, execution_timeframe=r.execution_timeframe,
        higher_timeframes=r.higher_timeframes or [],
        risk_reward_ratio=r.risk_reward_ratio, stop_loss_type=r.stop_loss_type,
        session_filters=r.session_filters or [],
        shared_by_username=r.sharer,
    )


@router.post("/shared/{token}/import", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
async def import_shared_strategy(
    token: str,
    current_user: User = Depends(require_paid_user_with_2fa),
    db: AsyncSession = Depends(get_db),
):
    src_res = await db.execute(
        select(Strategy).where(Strategy.share_token == token)
    )
    src = src_res.scalar_one_or_none()
    if not src:
        raise HTTPException(status_code=404, detail="That share link is invalid or has been revoked.")
    # Don't import your own — just return the existing one
    if str(src.user_id) == str(current_user.id):
        raise HTTPException(status_code=400, detail="This is already your own strategy.")
    # Load the original sharer's username for attribution
    src_user = (await db.execute(select(User).where(User.id == src.user_id))).scalar_one_or_none()
    sharer_name = src_user.username if src_user else "another trader"

    copy = Strategy(
        user_id=current_user.id,
        name=src.name + " (shared)",
        description=(src.description or "") + f"\n\n---\n📤 Shared by @{sharer_name}",
        instruments=src.instruments,
        primary_timeframe=src.primary_timeframe,
        execution_timeframe=src.execution_timeframe,
        higher_timeframes=src.higher_timeframes,
        risk_reward_ratio=src.risk_reward_ratio,
        stop_loss_type=src.stop_loss_type,
        stop_loss_ticks=src.stop_loss_ticks,
        max_contracts=src.max_contracts,
        session_filters=src.session_filters,
        fvg_min_size_ticks=src.fvg_min_size_ticks,
        fvg_max_size_ticks=src.fvg_max_size_ticks,
        rule_tree=src.rule_tree or {},
    )
    # Copy options-mode config if present (column may not exist on older deployments)
    for col in ("options_mode", "options_risk_per_trade_pct", "options_min_dte",
                "options_max_dte", "options_target_delta_min", "options_target_delta_max",
                "options_prefer_itm", "options_spread_width",
                "options_breakout_volume_mult", "options_avoid_earnings_days"):
        try:
            v = getattr(src, col, None)
            if v is not None:
                setattr(copy, col, v)
        except Exception:
            pass
    db.add(copy)
    await db.commit()
    await db.refresh(copy)

    return StrategyResponse(
        id=str(copy.id), name=copy.name, description=copy.description,
        status=copy.status.value, instruments=copy.instruments,
        primary_timeframe=copy.primary_timeframe, execution_timeframe=copy.execution_timeframe,
        higher_timeframes=copy.higher_timeframes or [],
        risk_reward_ratio=copy.risk_reward_ratio, stop_loss_type=copy.stop_loss_type,
        session_filters=copy.session_filters,
        starred=False,
        asset_class=classify_asset_class(copy.instruments),
        created_at=copy.created_at.isoformat(),
    )
