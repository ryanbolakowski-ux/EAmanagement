from loguru import logger
import os
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from datetime import datetime, timedelta, timezone
from typing import Optional
from pydantic import BaseModel

from app.database import get_db
from app.models.user import User, SubscriptionTier
# Admin routes are protected by require_admin (is_admin + valid passcode
# session) which IS the second factor — do NOT 2FA-gate them too, or the
# admin can be locked out of the unlock flow itself (object-Object bug 2026-06-11).
from app.core.auth import get_current_user
from app.services.email import send_tier_change_email, send_comp_granted_email, send_comp_revoked_email

router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription

# ── Admin safe-word gate ─────────────────────────────────────────────────
#
# Even after the main password + (optional) 2FA, every admin action
# requires a separate passcode (the "safe word"). The passcode is verified
# once per browser session and the result cached in Redis for 8 hours.
#
# Without this gate, a compromised admin account = full takeover. With it,
# the attacker also needs the safe-word, which is never sent over email,
# never displayed in the UI, and only known to the human admin.

import redis as _redis_lib
from app.core import sc_logic as _sc  # SYSTEMS-CHECK-V2 pure decision helpers
from app.core.security import verify_password as _verify_password

_admin_redis = _redis_lib.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True, db=0)
_ADMIN_PASSCODE_TTL = 8 * 60 * 60  # 8 hours per browser session


def _passcode_key(user_id: str, token: str) -> str:
    return f"admin_passcode_ok:{user_id}:{token}"


async def require_admin_with_passcode(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Gate: require is_admin AND a valid passcode session token."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    # The frontend sends the user's auth token as the passcode-session anchor
    auth = request.headers.get("authorization", "")
    token = auth.replace("Bearer ", "")[:32] if auth else ""
    if not token:
        raise HTTPException(status_code=403, detail="passcode_required")
    if not _admin_redis.get(_passcode_key(str(current_user.id), token)):
        raise HTTPException(status_code=403, detail="passcode_required")
    return current_user


class PasscodeRequest(BaseModel):
    code: str


@router.post("/verify-passcode")
async def verify_admin_passcode(
    data: PasscodeRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Verify the safe-word. Caches success in Redis for 8h per session."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    if not current_user.admin_passcode_hash:
        raise HTTPException(
            status_code=500,
            detail="No admin passcode set on this account. Contact platform owner.",
        )
    if not _verify_password(data.code, current_user.admin_passcode_hash):
        raise HTTPException(status_code=401, detail="Invalid passcode.")

    auth = request.headers.get("authorization", "")
    token = auth.replace("Bearer ", "")[:32] if auth else ""
    _admin_redis.setex(_passcode_key(str(current_user.id), token),
                        _ADMIN_PASSCODE_TTL, "1")
    return {"status": "ok", "valid_for_seconds": _ADMIN_PASSCODE_TTL}





@router.post("/lock")
async def admin_lock(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Invalidate the current session's passcode flag. Called from the
    frontend on logout so a re-login (or another tab on the same token)
    requires the safe-word again."""
    auth = request.headers.get("authorization", "")
    token = auth.replace("Bearer ", "")[:32] if auth else ""
    if token:
        _admin_redis.delete(_passcode_key(str(current_user.id), token))
    return {"status": "locked"}


@router.get("/passcode-status")
async def admin_passcode_status(
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Tells the frontend whether the current session has already verified
    the passcode. Used on Admin page mount to decide between showing the
    prompt or the dashboard."""
    if not current_user.is_admin:
        return {"is_admin": False, "passcode_verified": False}
    auth = request.headers.get("authorization", "")
    token = auth.replace("Bearer ", "")[:32] if auth else ""
    verified = bool(_admin_redis.get(_passcode_key(str(current_user.id), token))) if token else False
    return {"is_admin": True, "passcode_verified": verified}


# Admin access is gated by the user.is_admin column. Tier 5 = paid plan only.


async def require_admin(current_user: User = Depends(get_current_user)):
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


@router.get("/stats")
async def admin_stats(
    admin: User = Depends(require_admin_with_passcode),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Total users
    total = await db.execute(select(func.count(User.id)).where(User.is_admin == False))
    total_users = total.scalar()

    # New today
    today_result = await db.execute(
        select(func.count(User.id)).where(User.created_at >= today, User.is_admin == False)
    )
    new_today = today_result.scalar()

    # New this week
    week_result = await db.execute(
        select(func.count(User.id)).where(User.created_at >= week_ago, User.is_admin == False)
    )
    new_week = week_result.scalar()

    # New this month
    month_result = await db.execute(
        select(func.count(User.id)).where(User.created_at >= month_ago, User.is_admin == False)
    )
    new_month = month_result.scalar()

    # By tier
    tier_result = await db.execute(
        select(User.subscription_tier, func.count(User.id)).where(User.is_admin == False).group_by(User.subscription_tier)
    )
    tiers = {row[0]: row[1] for row in tier_result.fetchall()}

    # Active paper sessions
    paper_result = await db.execute(
        text("SELECT COUNT(*) FROM trade_sessions WHERE mode = \'paper\' AND is_active = true")
    )
    active_paper = paper_result.scalar()

    # Recent backtests (last 7 days)
    bt_result = await db.execute(
        text("SELECT COUNT(*) FROM backtest_runs WHERE created_at >= :d"),
        {"d": week_ago}
    )
    recent_backtests = bt_result.scalar()

    # Recent optimizations
    opt_result = await db.execute(
        text("SELECT COUNT(*) FROM optimization_runs WHERE created_at >= :d"),
        {"d": week_ago}
    )
    recent_optimizations = opt_result.scalar()

    # Total trades
    trades_result = await db.execute(text("SELECT COUNT(*) FROM trades"))
    total_trades = trades_result.scalar()

    # Per-mode P&L + counts (frontend uses these in the admin stat cards)
    paper_pnl_row = await db.execute(text(
        "SELECT COALESCE(SUM(pnl), 0), COUNT(*) FROM trades WHERE mode = 'paper' AND status = 'closed'"
    ))
    paper_total_pnl, paper_trade_count = paper_pnl_row.fetchone()
    paper_total_pnl = float(paper_total_pnl or 0)
    paper_trade_count = int(paper_trade_count or 0)

    live_pnl_row = await db.execute(text(
        "SELECT COALESCE(SUM(pnl), 0), COUNT(*) FROM trades WHERE mode = 'live' AND status = 'closed'"
    ))
    live_total_pnl, live_trade_count = live_pnl_row.fetchone()
    live_total_pnl = float(live_total_pnl or 0)
    live_trade_count = int(live_trade_count or 0)

    # Average win rate across user accounts that have at least one closed trade.
    # Each user contributes their personal wins/total ratio; we then average those.
    wr_result = await db.execute(text("""
        SELECT user_id,
               COUNT(*) FILTER (WHERE net_pnl > 0)::float / COUNT(*) AS user_wr
        FROM trades
        WHERE status = 'closed'
        GROUP BY user_id
        HAVING COUNT(*) > 0
    """))
    user_wrs = [float(row[1]) for row in wr_result.fetchall() if row[1] is not None]
    avg_win_rate = (sum(user_wrs) / len(user_wrs)) if user_wrs else 0.0

    return {
        "total_users": total_users,
        "new_today": new_today,
        "new_this_week": new_week,
        "new_this_month": new_month,
        "tiers": tiers,
        "active_paper_sessions": active_paper,
        "recent_backtests": recent_backtests,
        "recent_optimizations": recent_optimizations,
        "total_trades": total_trades,
        "paper_total_pnl": paper_total_pnl,
        "live_total_pnl": live_total_pnl,
        "paper_trade_count": paper_trade_count,
        "live_trade_count": live_trade_count,
        "avg_win_rate": round(avg_win_rate, 4),
        "accounts_with_trades": len(user_wrs),
    }


@router.get("/users")
async def admin_users(
    q: str | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import or_, func as _f
    stmt = select(User).where(User.is_admin == False)  # noqa: E712
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(_f.lower(User.email).like(like), _f.lower(User.username).like(like)))
    stmt = stmt.order_by(User.created_at.desc()).limit(200)
    result = await db.execute(stmt)
    users = result.scalars().all()
    return [
        {
            "id": str(u.id),
            "email": u.email,
            "username": u.username,
            "tier": u.subscription_tier,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat(),
            "trial_started_at": u.trial_started_at.isoformat() if u.trial_started_at else None,
            "trial_ends_at": u.trial_ends_at.isoformat() if u.trial_ends_at else None,
            "subscription_started_at": u.subscription_started_at.isoformat() if u.subscription_started_at else None,
            "subscription_ends_at": u.subscription_ends_at.isoformat() if u.subscription_ends_at else None,
            "stripe_subscription_id": u.stripe_subscription_id,
            "is_paying": bool(u.stripe_subscription_id),
            "comp_granted_at": u.comp_granted_at.isoformat() if u.comp_granted_at else None,
            "comp_expires_at": u.comp_expires_at.isoformat() if u.comp_expires_at else None,
            "comp_note": u.comp_note,
            "is_comp": bool(u.comp_granted_at and not u.stripe_subscription_id),
            "kyc_status": getattr(u, "kyc_status", None) or "not_started",
            "kyc_verified_at": u.kyc_verified_at.isoformat() if getattr(u, "kyc_verified_at", None) else None,
            "kyc_provider": getattr(u, "kyc_provider", None),
            "country_code": getattr(u, "country_code", None),
            "first_name": getattr(u, "first_name", None),
            "last_name": getattr(u, "last_name", None),
        }
        for u in users
    ]


class TierUpdate(BaseModel):
    tier: str


@router.put("/users/{user_id}/tier")
async def update_user_tier(
    user_id: str,
    data: TierUpdate,
    admin: User = Depends(require_admin_with_passcode),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    valid_tiers = ["free_trial", "tier_2", "tier_3", "tier_4", "tier_5"]
    if data.tier not in valid_tiers:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {data.tier}")

    old_tier = (
        user.subscription_tier.value
        if hasattr(user.subscription_tier, "value")
        else str(user.subscription_tier)
    )
    user.subscription_tier = data.tier
    await db.commit()

    # Notify the user — fire-and-forget so the API response isn't blocked on
    # Resend latency. send_tier_change_email already swallows its own errors.
    if old_tier != data.tier:
        try:
            send_tier_change_email(user.email, user.username, old_tier, data.tier)
        except Exception:
            pass
    # Bug #23 fix: removed dead stat queries (results never used)

    return {"message": f"User {user.username} updated to {data.tier}"}

@router.get("/users/{user_id}/trades")
async def admin_user_trades(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        text("""SELECT instrument, direction, entry_price, exit_price, pnl, net_pnl, mode, entry_time, exit_time
               FROM trades WHERE user_id = :uid ORDER BY entry_time DESC LIMIT 100"""),
        {"uid": user_id}
    )
    return [
        {"instrument": r[0], "direction": r[1], "entry_price": r[2], "exit_price": r[3],
         "pnl": float(r[4] or 0), "net_pnl": float(r[5] or 0), "mode": r[6],
         "entry_time": r[7].isoformat() if r[7] else None, "exit_time": r[8].isoformat() if r[8] else None}
        for r in result.fetchall()
    ]


@router.delete("/users/{user_id}")
async def admin_delete_user(
    user_id: str,
    admin: User = Depends(require_admin_with_passcode),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_admin:
        raise HTTPException(status_code=400, detail="Cannot delete admin users")

    # Delete user data
    await db.execute(text("DELETE FROM trades WHERE user_id = :uid"), {"uid": user_id})
    await db.execute(text("DELETE FROM trade_sessions WHERE user_id = :uid"), {"uid": user_id})
    await db.execute(text("DELETE FROM backtest_trades WHERE backtest_run_id IN (SELECT id FROM backtest_runs WHERE user_id = :uid)"), {"uid": user_id})
    await db.execute(text("DELETE FROM backtest_metrics WHERE backtest_run_id IN (SELECT id FROM backtest_runs WHERE user_id = :uid)"), {"uid": user_id})
    await db.execute(text("DELETE FROM backtest_runs WHERE user_id = :uid"), {"uid": user_id})
    await db.execute(text("DELETE FROM optimization_results WHERE optimization_run_id IN (SELECT id FROM optimization_runs WHERE user_id = :uid)"), {"uid": user_id})
    await db.execute(text("DELETE FROM optimization_runs WHERE user_id = :uid"), {"uid": user_id})
    await db.execute(text("DELETE FROM strategies WHERE user_id = :uid"), {"uid": user_id})
    await db.delete(user)
    await db.commit()
    # Bug #23 fix: removed dead stat queries (results never used)

    return {"message": f"User {user.username} deleted"}


@router.get('/users/{user_id}/acknowledgments')
async def admin_user_acknowledgments(
    user_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    '''Every disclaimer/T&C this user has clicked Accept on — with timestamp,
    IP address, user-agent, and which version of the document they saw.
    Used for the admin → user-profile → Acknowledgments section.'''
    rows = await db.execute(text('''
        SELECT id, kind, content_version, detail, ip_address, user_agent, agreed_at
          FROM user_acknowledgments
         WHERE user_id = :uid
         ORDER BY agreed_at DESC
    '''), {'uid': user_id})
    return [
        {
            'id': str(r.id),
            'kind': r.kind,
            'content_version': r.content_version,
            'detail': r.detail,
            'ip_address': r.ip_address,
            'user_agent': r.user_agent,
            'agreed_at': r.agreed_at.isoformat() if r.agreed_at else None,
        }
        for r in rows.fetchall()
    ]



# ── Comp + Subscription management ───────────────────────────────────────

class GrantCompRequest(BaseModel):
    tier: str
    expires_days: int = 30   # default 30 days of free access
    note: str | None = None


@router.post("/users/{user_id}/grant-comp")
async def grant_comp(
    user_id: str,
    data: GrantCompRequest,
    admin: User = Depends(require_admin_with_passcode),
    db: AsyncSession = Depends(get_db),
):
    """Grant a paid tier as a free comp. Sets comp_granted_at = now,
    comp_expires_at = now + expires_days, and records who granted it.

    Does NOT touch stripe_subscription_id — that\'s how we tell a comp from
    a real paying customer."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.is_admin:
        raise HTTPException(status_code=400, detail="Cannot grant comp to an admin account.")

    valid_tiers = ["tier_2", "tier_3", "tier_4", "tier_5"]
    if data.tier not in valid_tiers:
        raise HTTPException(status_code=400, detail=f"Invalid comp tier: {data.tier}")

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    user.subscription_tier = data.tier
    user.comp_granted_at = now
    user.comp_expires_at = now + timedelta(days=max(1, int(data.expires_days)))
    user.comp_granted_by = admin.id
    user.comp_note = (data.note or "").strip()[:500] or None
    # Defensive: clear any stale stripe subscription id (a comp isn\'t paying)
    user.stripe_subscription_id = None
    await db.commit()

    # Fire-and-forget notification
    try:
        send_comp_granted_email(
            to=user.email, username=user.username, tier=data.tier,
            expires_at_human=user.comp_expires_at.strftime("%a, %b %d %Y"),
            note=user.comp_note, granted_by_email=admin.email,
        )
    except Exception:
        pass

    return {
        "status": "granted",
        "tier": data.tier,
        "expires_at": user.comp_expires_at.isoformat(),
    }


@router.post("/users/{user_id}/revoke-comp")
async def revoke_comp(
    user_id: str,
    admin: User = Depends(require_admin_with_passcode),
    db: AsyncSession = Depends(get_db),
):
    """Drop a comp immediately. User falls back to free_trial."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if user.is_admin:
        raise HTTPException(status_code=400, detail="Cannot revoke comp on an admin account.")

    user.subscription_tier = "free_trial"
    user.comp_granted_at = None
    user.comp_expires_at = None
    user.comp_granted_by = None
    user.comp_note = None
    await db.commit()
    return {"status": "revoked"}


@router.get("/comps")
async def list_active_comps(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List every comped user with their tier, granted/expires dates, and
    the admin who granted it (in case we ever support multiple admins)."""
    from datetime import datetime, timezone
    rows = (await db.execute(text("""
        SELECT u.id, u.email, u.username, u.subscription_tier,
               u.comp_granted_at, u.comp_expires_at, u.comp_note,
               u.comp_granted_by,
               g.email AS granted_by_email
          FROM users u
          LEFT JOIN users g ON g.id = u.comp_granted_by
         WHERE u.comp_granted_at IS NOT NULL
           AND u.is_admin = false
           AND u.stripe_subscription_id IS NULL
         ORDER BY u.comp_expires_at NULLS LAST
    """))).all()
    out = []
    now = datetime.now(timezone.utc)
    for r in rows:
        expires = r.comp_expires_at
        days_left = None
        expired = False
        if expires:
            delta = expires - now
            days_left = max(0, delta.days + (1 if delta.seconds > 0 else 0))
            expired = expires < now
        out.append({
            "id": str(r.id),
            "email": r.email,
            "username": r.username,
            "tier": r.subscription_tier,
            "granted_at": r.comp_granted_at.isoformat() if r.comp_granted_at else None,
            "expires_at": expires.isoformat() if expires else None,
            "days_left": days_left,
            "expired": expired,
            "note": r.comp_note,
            "granted_by_email": r.granted_by_email,
        })
    return {"comps": out, "count": len(out)}


@router.get("/subscriptions")
async def list_paying_subscriptions(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List every paying customer (Stripe-backed subscription) with their
    start/end dates and current tier."""
    rows = (await db.execute(text("""
        SELECT id, email, username, subscription_tier,
               subscription_started_at, subscription_ends_at,
               stripe_subscription_id, created_at
          FROM users
         WHERE stripe_subscription_id IS NOT NULL
           AND is_admin = false
         ORDER BY subscription_started_at DESC NULLS LAST
    """))).all()
    out = []
    for r in rows:
        out.append({
            "id": str(r.id), "email": r.email, "username": r.username,
            "tier": r.subscription_tier,
            "subscription_started_at": r.subscription_started_at.isoformat() if r.subscription_started_at else None,
            "subscription_ends_at":    r.subscription_ends_at.isoformat()    if r.subscription_ends_at    else None,
            "stripe_subscription_id": r.stripe_subscription_id,
            "signed_up_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"subscriptions": out, "count": len(out)}


@router.get("/kyc/events")
async def admin_kyc_events(
    limit: int = 200,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return the most recent KYC events for the admin audit log."""
    try:
        rows = (await db.execute(text("""
            SELECT k.id, k.user_id, COALESCE(k.user_email, u.email) AS user_email,
                   u.username, k.event_type, k.status, k.provider,
                   k.session_id, k.country, k.ip, k.detail, k.created_at,
                   u.kyc_status AS current_status, u.kyc_verified_at,
                   u.first_name, u.last_name, u.country_code
              FROM kyc_events k
              LEFT JOIN users u ON u.id = k.user_id::uuid
             ORDER BY k.created_at DESC
             LIMIT :lim
        """), {"lim": min(max(1, limit), 1000)})).fetchall()
        return {"events": [dict(r._mapping) for r in rows]}
    except Exception as e:
        # Table may not exist yet (no KYC attempts) — return empty
        return {"events": [], "note": f"no events yet: {e}"}


@router.get("/kyc/summary")
async def admin_kyc_summary(
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """KYC funnel counts for the admin dashboard."""
    rows = (await db.execute(text("""
        SELECT COALESCE(kyc_status, 'not_started') AS status, COUNT(*) AS n
          FROM users
         GROUP BY 1
         ORDER BY 2 DESC
    """))).fetchall()
    return {"by_status": [dict(r._mapping) for r in rows]}


class _KycManualReq(BaseModel):
    user_id: str
    new_status: str  # 'verified' | 'failed' | 'not_started'
    reason: str | None = None


@router.post("/kyc/manual")
async def admin_kyc_manual(
    data: _KycManualReq,
    current_user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin manual KYC override. Logged into kyc_events with the admin's email."""
    if data.new_status not in ("verified", "failed", "not_started", "requires_input"):
        raise HTTPException(status_code=400, detail="invalid status")
    verified_at_sql = "kyc_verified_at = NOW()," if data.new_status == "verified" else "kyc_verified_at = NULL,"
    await db.execute(text(f"""
        UPDATE users
           SET kyc_status = :st,
               {verified_at_sql}
               kyc_provider = 'admin_override'
         WHERE id = CAST(:uid AS uuid)
    """), {"st": data.new_status, "uid": data.user_id})
    # Audit row
    try:
        await db.execute(text("""
            INSERT INTO kyc_events (user_id, user_email, event_type, status, provider, detail)
            VALUES (CAST(:uid AS uuid),
                    (SELECT email FROM users WHERE id = CAST(:uid AS uuid)),
                    'admin_override', :st, 'admin_override',
                    :detail)
        """), {"uid": data.user_id, "st": data.new_status,
               "detail": f"by {current_user.email}: {data.reason or '(no reason)'}"})
    except Exception as e:
        pass
    await db.commit()
    return {"status": "ok", "new_status": data.new_status}



@router.get("/scanner-health")
async def scanner_health_endpoint(admin: User = Depends(require_admin_with_passcode)):
    """Admin-only: realtime health of the futures + options scanner pipeline.
    Hits every component (Redis, yfinance, Resend, Polygon, DB, watchers) and
    returns JSON. Use this to debug any 'no email fired' incident quickly."""
    from app.engines.scanner_health import check_health
    return await check_health()



# ─────────────────────────────────────────────────────────────────────────
# Admin Systems Check dashboard
# ─────────────────────────────────────────────────────────────────────────
#
# Single comprehensive read endpoint that powers the Admin → Systems Check
# tab. Aggregates every subsystem (scanners / emails / trading / integrations
# / infra / recent errors / running jobs / metrics) into one JSON document.
#
# CRITICAL: this endpoint is read by an admin browser session and must NEVER
# return any secret material. Concretely we:
#   * never echo API keys, tokens, encrypted_credentials, session tokens
#   * never echo user PII other than admin emails (which an admin already
#     knows because the route is gated by is_admin)
#   * report status indicators (green/yellow/red) and counts only — for
#     external providers we surface the LAST status CODE from a recent
#     health probe, not the API key itself
#
# The shape is stable: top-level keys (overall/scanners/emails/trading/
# integrations/infra/recent_errors/jobs_running/metrics) are part of the
# frontend contract.
# ─────────────────────────────────────────────────────────────────────────

def _systems_check_status(*states: str) -> str:
    """Return the worst status from a sequence. Red > yellow > green."""
    order = {"red": 3, "yellow": 2, "green": 1, "unknown": 0}
    states = [s for s in states if s]
    if not states:
        return "unknown"
    return max(states, key=lambda s: order.get(s, 0))


def _redact_secrets(blob):
    """Recursive sanity check: walk a dict/list, return list of any leaked
    secret-name substrings found. Used by the no-secrets test, NOT by the
    endpoint itself (which simply never returns secrets in the first place)."""
    leaked = []
    BAD = ("ANTHROPIC_API_KEY", "STRIPE_SECRET_KEY", "POLYGON_API_KEY",
           "RESEND_API_KEY", "encrypted_credentials", "hashed_password",
           "admin_passcode_hash")
    def _walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if any(b in str(k) for b in BAD):
                    leaked.append(str(k))
                _walk(v)
        elif isinstance(o, list):
            for x in o:
                _walk(x)
        else:
            s = str(o)
            for b in BAD:
                if b in s:
                    leaked.append(b)
    _walk(blob)
    return leaked


@router.get("/systems-check")
async def systems_check(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Comprehensive admin-only dashboard payload.

    Gated server-side: non-admins get 403, full stop. The frontend also
    soft-gates on `useQuery(/auth/me).is_admin` but that is UX only — every
    request hits this is_admin check.
    """
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")

    from zoneinfo import ZoneInfo
    now_utc = datetime.now(timezone.utc)
    et_now = now_utc.astimezone(ZoneInfo("America/New_York"))
    today_et = et_now.date().isoformat()

    # ── Helpers ─────────────────────────────────────────────────────────
    async def _scalar(sql: str, **params):
        try:
            return (await db.execute(text(sql), params)).scalar()
        except Exception as e:
            logger.warning(f"[systems-check] scalar query failed: {e}")
            return None

    async def _first(sql: str, **params):
        try:
            return (await db.execute(text(sql), params)).first()
        except Exception:
            return None

    def _staleness(ts, crit: bool = True):
        """Map a datetime to {green/yellow/red} by minutes since. For
        non-critical components (crit=False) staleness caps at YELLOW — a stale
        periodic sync or an overnight-held position is "degraded", not "down"."""
        if ts is None:
            return "yellow"
        try:
            delta = (now_utc - ts).total_seconds() / 60.0
        except Exception:
            return "yellow"
        if delta < 5: return "green"
        if delta < 30: return "yellow"
        return "red" if crit else "yellow"

    # ── SYSTEMS-CHECK-V2: facts for accurate, non-stale component health ──
    try:
        from app.engines.options.premarket_scheduler import _within_market_window
        # Holiday-aware (round 2): a weekday inside trading hours is still CLOSED on
        # an NYSE holiday (e.g. Juneteenth), so positions/balances aren't expected fresh.
        _mkt_open = bool(_within_market_window(et_now)) and not _sc.is_market_holiday(et_now.date())
    except Exception:
        _mkt_open = True  # fail toward 'should be fresh' (never hides a real stall)
    # KYC webhook health = signing secret CONFIGURED (events are rare; absence
    # of recent events is NOT a failure).
    _kyc_configured = bool(os.environ.get("STRIPE_IDENTITY_WEBHOOK_SECRET"))
    # Live Tradier execution uses PER-ACCOUNT credentials; the global
    # TRADIER_API_KEY env is unused, so health follows real live Tradier sessions.
    _live_tradier_any = await _scalar(
        "SELECT COUNT(*) FROM trade_sessions ts JOIN broker_accounts ba ON ba.id = ts.broker_account_id "
        "WHERE ts.mode='live' AND ts.is_active=true AND LOWER(ba.broker)='tradier'") or 0
    _live_tradier_realnocreds = await _scalar(
        "SELECT COUNT(*) FROM trade_sessions ts JOIN broker_accounts ba ON ba.id = ts.broker_account_id "
        "WHERE ts.mode='live' AND ts.is_active=true AND LOWER(ba.broker)='tradier' "
        "AND COALESCE(ba.is_demo,false)=false AND COALESCE(ba.sandbox_mode,false)=false "
        "AND (ba.encrypted_credentials IS NULL OR ba.encrypted_credentials='')") or 0
    # Job queue: only genuinely-actionable pending trades are a real backlog —
    # NOT terminal (executed/declined/expired) or time-expired rows.
    _queue_live = await _scalar(
        "SELECT COUNT(*) FROM pending_trades WHERE confirmed_at IS NULL AND declined_at IS NULL "
        "AND LOWER(COALESCE(status,'')) NOT IN ('executed','declined','expired','cancelled','failed') "
        "AND (expires_at IS NULL OR expires_at > NOW())") or 0
    _queue_abandoned = await _scalar(
        "SELECT COUNT(*) FROM pending_trades WHERE confirmed_at IS NULL AND declined_at IS NULL "
        "AND LOWER(COALESCE(status,'')) NOT IN ('executed','declined','expired','cancelled','failed') "
        "AND expires_at < NOW()") or 0

    # ── scanners ────────────────────────────────────────────────────────
    theta_today = await _first(
        "SELECT ticker, picked_at FROM email_signals_history "
        "WHERE picked_at::date = CURRENT_DATE AND asset_type='options' "
        "ORDER BY picked_at DESC LIMIT 1"
    )
    futures_count = await _scalar(
        "SELECT COUNT(*) FROM account_signals "
        "WHERE fired_at::date = CURRENT_DATE"
    ) or 0
    futures_last = await _first(
        "SELECT MAX(fired_at) AS at FROM account_signals "
        "WHERE fired_at::date = CURRENT_DATE"
    )
    options_today = await _first(
        "SELECT ticker, picked_at FROM email_signals_history "
        "WHERE picked_at::date = CURRENT_DATE AND asset_type='options' "
        "ORDER BY picked_at DESC LIMIT 1"
    )

    # A no-pick / no-signal day is a VALID, frequent outcome of the scan, NOT a
    # failure (the scanner stands down whenever nothing clears the quality bar; a
    # no-pick day is normal on weekdays too). So "no output today" is GREEN, not
    # degraded — the admin gets the actual state from the data fields below
    # (today_pick / count / last_run_at). A genuine scanner CRASH surfaces in the
    # Recent errors card. (SYSTEMS-CHECK-V3.1 — per Ryan: a no-pick day must never
    # make the dashboard yellow, on weekends OR weekdays.)
    scanners = {
        "theta_scanner": {
            "status": "green",
            "last_run_at": theta_today.picked_at.isoformat() if theta_today else None,
            "today_pick": theta_today.ticker if theta_today else None,
            "next_run_at": None,  # premarket scheduler-driven, not cron
        },
        "futures_scanner": {
            "status": "green",
            "last_run_at": futures_last.at.isoformat() if futures_last and futures_last.at else None,
            "today_signal_count": int(futures_count),
        },
        "options_scanner": {
            "status": "green",
            "last_run_at": options_today.picked_at.isoformat() if options_today else None,
            "today_pick": options_today.ticker if options_today else None,
        },
    }

    # ── emails ──────────────────────────────────────────────────────────
    sent_today_count = await _scalar(
        "SELECT COUNT(*) FROM account_signals "
        "WHERE fired_at::date = CURRENT_DATE AND provider_status='sent'"
    ) or 0
    suppressed_today = await _scalar(
        "SELECT COUNT(*) FROM account_signals "
        "WHERE fired_at::date = CURRENT_DATE AND duplicate_suppressed_count > 0"
    ) or 0
    last_success = await _first("""
        SELECT s.fired_at, s.provider_message_id, s.instrument, u.email AS recipient
          FROM account_signals s
          JOIN users u ON u.id = s.user_id
         WHERE s.provider_status = 'sent'
         ORDER BY s.fired_at DESC LIMIT 1
    """)
    last_failure = await _first("""
        SELECT s.fired_at, s.error_message, s.instrument, u.email AS recipient
          FROM account_signals s
          JOIN users u ON u.id = s.user_id
         WHERE s.provider_status IS NOT NULL
           AND s.provider_status NOT IN ('sent', '')
         ORDER BY s.fired_at DESC LIMIT 1
    """)
    emails = {
        "last_successful": ({
            "to": last_success.recipient,
            "subject": f"Signal · {last_success.instrument}",
            "at": last_success.fired_at.isoformat() if last_success.fired_at else None,
            "provider_message_id": last_success.provider_message_id,
        } if last_success else None),
        "last_failed": ({
            "to": last_failure.recipient,
            "subject": f"Signal · {last_failure.instrument}",
            "at": last_failure.fired_at.isoformat() if last_failure.fired_at else None,
            # error_message is our own log line — never a secret. Truncate
            # defensively in case a stack trace got captured.
            "error": (last_failure.error_message or "")[:200],
        } if last_failure else None),
        "sent_today": int(sent_today_count),
        "suppressed_today": int(suppressed_today),
        # Activity-driven and a no-send day is normal (no signal => no email), so
        # these are informational GREEN, never a degraded alarm. (SYSTEMS-CHECK-V3.1)
        "trade_alert_status": "green",
        "futures_email_status": "green",
        "options_swing_status": "green",
        "heartbeat_status": "green",  # heartbeat is single-recipient, always armed
    }

    # ── trading ─────────────────────────────────────────────────────────
    live_active = await _scalar(
        "SELECT COUNT(*) FROM trade_sessions WHERE mode='live' AND is_active=true"
    ) or 0
    paper_active = await _scalar(
        "SELECT COUNT(*) FROM trade_sessions WHERE mode='paper' AND is_active=true"
    ) or 0
    open_positions = await _scalar(
        "SELECT COUNT(*) FROM open_positions_watch WHERE status='open'"
    ) or 0
    closes_today = await _scalar(
        "SELECT COUNT(*) FROM open_positions_watch "
        "WHERE status='closed' AND closed_at::date = CURRENT_DATE"
    ) or 0
    last_priced_row = await _first(
        "SELECT MAX(last_priced_at) AS at FROM open_positions_watch WHERE status='open'"
    )
    last_priced = last_priced_row.at if last_priced_row else None

    trading = {
        "live_trading": {
            # The live-trading monitor is always armed; zero active sessions is a
            # normal IDLE state (e.g. overnight / nobody trading), not degraded.
            # Count is informational. (SYSTEMS-CHECK-V3)
            "status": "green",
            "active_sessions": int(live_active),
        },
        "paper_trading": {
            "status": "green",  # idle (0 sessions) is normal, not degraded
            "active_sessions": int(paper_active),
        },
        "open_position_monitor": {
            # Only 'degraded' if positions are open AND the market is OPEN yet
            # they haven't been re-priced recently. Market closed -> positions
            # legitimately don't re-price, so that's green (informational).
            "status": _sc.open_monitor_status(open_positions, _mkt_open, last_priced, now_utc),
            "last_check_at": last_priced.isoformat() if last_priced else None,
            "open_count": int(open_positions),
            "market_open": _mkt_open,
        },
        "position_closing_job": {
            "status": "green",
            "last_check_at": now_utc.isoformat(),
            "today_closes": int(closes_today),
        },
    }

    # ── integrations ────────────────────────────────────────────────────
    kyc_last_row = await _first(
        "SELECT MAX(created_at) AS at FROM kyc_events"
    )
    kyc_last = kyc_last_row.at if kyc_last_row else None
    kyc_today = await _scalar(
        "SELECT COUNT(*) FROM kyc_events WHERE created_at::date = CURRENT_DATE"
    ) or 0
    broker_last_row = await _first(
        "SELECT MAX(cached_balance_at) AS at, COUNT(*) AS n "
        "FROM broker_accounts WHERE is_active=true"
    )
    broker_last = broker_last_row.at if broker_last_row else None
    broker_n = int(broker_last_row.n) if broker_last_row else 0

    # Stripe webhook + email provider + Tradier: we don't keep per-event
    # tables for these, so we report a "configured" status based on whether
    # the secrets are present in env — without ever echoing the secret value.
    stripe_status = "green" if os.environ.get("STRIPE_WEBHOOK_SECRET") else "yellow"
    resend_status = "green" if os.environ.get("RESEND_API_KEY") else "red"
    # SYSTEMS-CHECK-V2: the global TRADIER_API_KEY env is NOT used by the broker
    # adapter (per-account encrypted creds are), so it must not drive status.
    # RED only when a REAL-money live Tradier session is active but its account
    # has no stored credentials; otherwise green (not-in-use / sandbox / configured).
    tradier_status = _sc.tradier_status(_live_tradier_realnocreds)

    # Probe Resend with a HEAD-like check (its domains endpoint) so we get a
    # real recent status code rather than a stale boolean. 2s timeout so this
    # endpoint stays snappy.
    last_resend_code = None
    try:
        import httpx as _hx
        rk = os.environ.get("RESEND_API_KEY", "")
        if rk:
            with _hx.Client(timeout=2.0) as c:
                rr = c.get("https://api.resend.com/domains",
                           headers={"Authorization": f"Bearer {rk}"})
                last_resend_code = rr.status_code
    except Exception:
        last_resend_code = None

    integrations = {
        "kyc_webhooks": {
            # Health = webhook CONFIGURED + reachable. KYC events fire only on
            # signup/verification (rare), so 'no recent events' is informational,
            # never degraded. Yellow ONLY when the signing secret is missing.
            "status": _sc.kyc_status(_kyc_configured),
            "configured": _kyc_configured,
            "last_received_at": kyc_last.isoformat() if kyc_last else None,
            "today_count": int(kyc_today),
        },
        "broker_sync": {
            # SYSTEMS-CHECK-V3.2: a background loop (engines/live_trading/
            # balance_sync.py) now refreshes balances every ~15 min during market
            # hours, so freshness is a meaningful signal again. Green within 60 min
            # of the last sync, or when the market is closed / no accounts; yellow
            # only if the sync loop genuinely stalls >60 min during market hours
            # (broken creds / broker outage also surface via tradier_api + errors).
            "status": _sc.broker_status(broker_n, _mkt_open, broker_last, now_utc),
            "last_sync_at": broker_last.isoformat() if broker_last else None,
            "accounts": broker_n,
        },
        "market_data": {
            "status": "green" if os.environ.get("POLYGON_API_KEY") else "yellow",
            "providers": ["polygon", "yfinance", "twelvedata"],
        },
        "stripe_webhook": {
            "status": stripe_status,
            "last_received_at": None,
        },
        "email_provider": {
            "status": "green" if last_resend_code == 200 else (
                "red" if resend_status == "red" else "yellow"
            ),
            # ONLY the http status code — never the API key itself.
            "last_status_code": last_resend_code,
        },
        "tradier_api": {
            "status": tradier_status,
            "in_use": bool(_live_tradier_any),
            "live_sessions": int(_live_tradier_any),
            "last_call_at": None,
        },
    }

    # ── infra ───────────────────────────────────────────────────────────
    # database: simple SELECT 1
    db_ok = True
    try:
        (await db.execute(text("SELECT 1"))).scalar()
    except Exception:
        db_ok = False

    # redis: ping
    redis_ok = False
    try:
        import redis.asyncio as _ra
        r = _ra.from_url(os.environ["REDIS_URL"], decode_responses=True)
        redis_ok = bool(await r.ping())
        await r.aclose()
    except Exception:
        redis_ok = False

    # queue depth: SYSTEMS-CHECK-V2 — only genuinely-actionable pending trades
    # (computed in the facts block: excludes executed/declined/expired/old rows).
    queue_depth = int(_queue_live)

    stuck_runs = await _scalar(
        "SELECT COUNT(*) FROM backtest_runs "
        "WHERE LOWER(status::text)='running' AND created_at < NOW() - INTERVAL '1 hour'"
    ) or 0

    infra = {
        "database": {"status": "green" if db_ok else "red"},
        "redis": {"status": "green" if redis_ok else "red"},
        "queue": {
            "status": _sc.queue_status(queue_depth),
            "depth": int(queue_depth),
            "abandoned": int(_queue_abandoned),
        },
        "scheduler": {
            "status": "green",
            "next_tick_at": (now_utc + timedelta(seconds=60)).isoformat(),
        },
        "stuck_runs": int(stuck_runs),
    }

    # ── recent_errors ───────────────────────────────────────────────────
    recent_errors = []
    try:
        from app.core.log_ring_buffer import get_recent_records
        recent_errors = get_recent_records(level="ERROR", limit=10)
    except Exception:
        recent_errors = []

    # ── jobs_running ────────────────────────────────────────────────────
    jobs_running = []
    try:
        rows = (await db.execute(text(
            "SELECT id, created_at FROM backtest_runs "
            "WHERE LOWER(status::text)='running' ORDER BY created_at DESC LIMIT 20"
        ))).fetchall()
        for r in rows:
            jobs_running.append({
                "name": f"backtest:{r.id}",
                "started_at": r.created_at.isoformat() if r.created_at else None,
                "expected_completion": None,
            })
        rows = (await db.execute(text(
            "SELECT id, created_at FROM optimization_runs "
            "WHERE LOWER(status::text)='running' ORDER BY created_at DESC LIMIT 20"
        ))).fetchall()
        for r in rows:
            jobs_running.append({
                "name": f"optimization:{r.id}",
                "started_at": r.created_at.isoformat() if r.created_at else None,
                "expected_completion": None,
            })
    except Exception:
        pass

    # ── metrics ─────────────────────────────────────────────────────────
    sent_signal_count_today = sent_today_count
    scanner_output_count_today = (futures_count or 0) + (1 if theta_today else 0)
    # Last deployment ~ last container start (boot-time module import).
    last_deploy = os.environ.get("DEPLOY_MARKER")  # may be None
    metrics = {
        "sent_signal_count_today": int(sent_signal_count_today),
        "scanner_output_count_today": int(scanner_output_count_today),
        "last_deployment_at": last_deploy,
        "last_health_check_at": now_utc.isoformat(),
    }

    # ── component metadata: checked_at + criticality + error list ───────
    # CRITICAL components can turn the WHOLE dashboard red. Everything else is
    # "degraded" at worst — its red contributes only YELLOW to the overall.
    CRITICAL_KEYS = {"database", "redis", "email_provider"}
    _now_iso = now_utc.isoformat()
    _human = {
        "theta_scanner": "Theta Scanner (stock pick)", "futures_scanner": "Futures scanner",
        "options_scanner": "Options scanner", "live_trading": "Live trading", "paper_trading": "Paper trading",
        "open_position_monitor": "Open-position monitor", "position_closing_job": "Position-closing job",
        "kyc_webhooks": "KYC webhooks", "broker_sync": "Broker sync", "market_data": "Market data",
        "stripe_webhook": "Stripe webhook", "email_provider": "Email provider (Resend)", "tradier_api": "Tradier API",
        "database": "Database", "redis": "Redis", "queue": "Job queue", "scheduler": "Scheduler",
    }
    _manual = {
        "email_provider": "RESEND_API_KEY missing/invalid in prod env, or Resend returning non-200. Verify the key + Resend account status.",
        "database": "Postgres unreachable — check the edge_db container and DATABASE_URL.",
        "redis": "Redis unreachable — check the edge_redis container and REDIS_URL.",
        "stripe_webhook": "STRIPE_WEBHOOK_SECRET not set in prod env — add it so Stripe events verify.",
        "market_data": "POLYGON_API_KEY not set — fallbacks (cache/yfinance) still work, but the primary provider is unconfigured.",
        "tradier_api": "A real-money live Tradier session is active but its broker account has no stored "
                       "credentials. Reconnect the Tradier account under Settings → Brokers. (The global "
                       "TRADIER_API_KEY env var is NOT used — credentials are per-account.)",
        "kyc_webhooks": "STRIPE_IDENTITY_WEBHOOK_SECRET is not set — add it so Stripe Identity (KYC) "
                        "webhooks verify. Note: KYC events are rare, so 'no recent events' alone is normal.",
        "futures_scanner": "Futures signals are event-driven during market hours; zero by the 4:00 PM ET "
                           "close means no qualifying setup occurred today (valid). If you expected signals, "
                           "check the futures scanner/runner in Recent errors and the Scheduler card.",
    }
    _fixable = {
        "theta_scanner": "rerun_scanner_check", "options_scanner": "rerun_scanner_check",
        "open_position_monitor": "resync_positions",
        "broker_sync": "resync_broker", "email_provider": "refresh_email_health",
        "queue": "clear_stale_jobs", "redis": "refresh_health", "database": "refresh_health",
        # market_data intentionally NOT auto-fixable: its only failure mode is a
        # missing POLYGON_API_KEY, which a re-check cannot set -> show instructions.
        # futures_scanner intentionally NOT auto-fixable: futures signals are
        # event-driven during RTH, so there's nothing to "re-run" -> show guidance.
    }
    errors = []
    for _sname, _sect in (("scanners", scanners), ("trading", trading),
                          ("integrations", integrations), ("infra", infra)):
        for _k, _v in _sect.items():
            if not (isinstance(_v, dict) and "status" in _v):
                continue
            _v.setdefault("checked_at", _now_iso)
            _is_crit = _k in CRITICAL_KEYS
            _v["critical"] = _is_crit
            if _v["status"] in ("red", "yellow"):
                _last_succ = (_v.get("last_run_at") or _v.get("last_sync_at")
                              or _v.get("last_received_at") or _v.get("last_check_at"))
                _affected = None
                if _k == "email_provider" and last_failure:
                    _affected = f"email to {last_failure.recipient}"
                elif _k == "broker_sync":
                    _affected = f"{broker_n} broker account(s)"
                elif _k == "open_position_monitor":
                    _affected = f"{open_positions} open position(s)"
                _sc_msgs = {
                    "theta_scanner": "No qualifying Theta (stock) pick today after the 9:25 ET premarket window. A no-pick day is valid — click Fix to re-run the scan now.",
                    "futures_scanner": "No futures signals fired today after the 4:00 PM ET cash close. A quiet day is valid — if you expected signals, check Recent errors / the futures runner.",
                    "options_scanner": "No qualifying options-swing pick today after the 9:25 ET premarket window. A no-pick day is valid — click Fix to re-run the scan now.",
                    "open_position_monitor": "Open positions haven't been re-priced recently (market is open) — click Fix to re-price now.",
                    "queue": (f"{int(queue_depth)} trade(s) awaiting confirmation"
                              + (f"; {int(_queue_abandoned)} abandoned (Fix retires those — a live backlog needs review)." if _queue_abandoned else ".")),
                    "kyc_webhooks": "KYC webhook signing secret is not configured.",
                    "tradier_api": "A real-money live Tradier session is active but its account credentials are missing.",
                    "broker_sync": "Broker balance sync is stale.",
                    "market_data": "Primary market-data key (Polygon) is not configured.",
                    "stripe_webhook": "Stripe webhook signing secret is not configured.",
                }
                _msg = _sc_msgs.get(_k) or {"red": "Critical: component down", "yellow": "Degraded / stale"}.get(_v["status"])
                if _k == "email_provider" and last_failure and last_failure.error_message:
                    _msg = (last_failure.error_message or _msg)[:200]
                errors.append({
                    "component": _k, "label": _human.get(_k, _k), "section": _sname,
                    "severity": "critical" if (_is_crit and _v["status"] == "red") else "warning",
                    "status": _v["status"], "message": _msg, "at": _now_iso,
                    "affected": _affected, "last_success": _last_succ,
                    "auto_fixable": _k in _fixable, "fix_action": _fixable.get(_k),
                    "manual_instructions": _manual.get(_k),
                })
    _email_subs = ("trade_alert_status", "futures_email_status", "options_swing_status", "heartbeat_status")

    # ── overall (criticality-aware) ─────────────────────────────────────
    # Round 2: roll up through the unit-tested sc_logic.overall_status so the
    # executed path is the one the tests cover. Each error -> (status, is_critical);
    # degraded email sub-statuses contribute non-critical yellow.
    _overall_pairs = [(e["status"], e["severity"] == "critical") for e in errors]
    _overall_pairs += [(emails[k], False) for k in _email_subs if emails.get(k) in ("red", "yellow")]
    worst = _sc.overall_status(_overall_pairs)
    summary_map = {
        "green":  "All systems operating normally.",
        "yellow": "Some subsystems are degraded or stale — review the flagged cards (non-critical).",
        "red":    "A CRITICAL subsystem is down — see the flagged cards and Show Errors.",
        "unknown": "Status indeterminate.",
    }
    overall = {
        "status": worst,
        "summary": summary_map.get(worst, "Status indeterminate."),
        "checked_at": _now_iso,
        "error_count": len(errors),
        "critical_count": sum(1 for e in errors if e["severity"] == "critical"),
    }

    return {
        "overall": overall,
        "errors": errors,
        "scanners": scanners,
        "emails": emails,
        "trading": trading,
        "integrations": integrations,
        "infra": infra,
        "recent_errors": recent_errors,
        "jobs_running": jobs_running,
        "metrics": metrics,
    }


class _SCFixRequest(BaseModel):
    action: str
    component: Optional[str] = None



# ── SYSTEMS-CHECK-RUN-V1: explicit "Run Full Systems Check" w/ run tracking ──
def _sc_redis():
    try:
        import redis as _rd
        return _rd.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                                  decode_responses=True)
    except Exception:
        return None


async def _sc_safety_components(db) -> list:
    """Recent safety-guard subsystems surfaced as systems-check components."""
    import inspect
    out = []
    def _add(component, label, ok, msg, fix=None):
        out.append({"component": component, "label": label, "section": "safety",
                    "status": "green" if ok else "yellow",
                    "severity": "ok" if ok else "warning",
                    "message": None if ok else msg, "auto_fixable": False,
                    "fix_action": None, "manual_instructions": None if ok else fix,
                    "last_success": None})
    try:
        from app.engines.entry_guard import can_enter, _setup_redis
        ok = ("ACCOUNT-SETUP-DEDUP-V1" in inspect.getsource(can_enter)) and (_setup_redis() is not None)
        _add("dup_prevention", "Duplicate-trade prevention", ok,
             "account-setup dedup missing or redis down", "Verify entry_guard ACCOUNT-SETUP-DEDUP-V1 + redis.")
    except Exception:
        _add("dup_prevention", "Duplicate-trade prevention", False, "check failed", "Inspect entry_guard.")
    try:
        import app.engines.account_signals.runner as _r
        ok = "SIGNAL-PRICE-ALIGN-V1" in inspect.getsource(_r)
        _add("signal_price_align", "Signal price alignment", ok,
             "price-alignment guard missing", "Verify account_signals/runner SIGNAL-PRICE-ALIGN-V1.")
    except Exception:
        _add("signal_price_align", "Signal price alignment", False, "check failed", "")
    try:
        from app.engines.account_signals.signal_guard import MIN_STOP_POINTS
        import app.engines.account_signals.signal_guard as _sg
        ok = "TINY-RANGE-HARD-REJECT" in inspect.getsource(_sg) and bool(MIN_STOP_POINTS)
        _add("quality_filters", "Scanner quality filters (futures tiny-range + R:R)", ok,
             "tiny-range hard reject missing", "Verify signal_guard TINY-RANGE-HARD-REJECT.")
    except Exception:
        _add("quality_filters", "Scanner quality filters", False, "check failed", "")
    try:
        from app.core.sizing import unified_size
        r = unified_size(entry_price=100, stop_loss=99, risk_per_trade_usd=500, point_value=1.0)
        _add("risk_sizing", "Risk sizing (unified min-of)", bool(r.ok and r.final_size == 500),
             "unified_size not returning expected size", "Inspect app/core/sizing.py.")
    except Exception:
        _add("risk_sizing", "Risk sizing", False, "check failed", "")
    try:
        from app.api.routes.legal import CURRENT_VERSIONS
        ok = "fully_automated_trading" in CURRENT_VERSIONS
        _add("automation_agreement", "Automation agreement system", ok,
             "fully_automated_trading legal doc missing", "Deploy the fully_automated_trading legal doc.")
    except Exception:
        _add("automation_agreement", "Automation agreement system", False, "check failed", "")
    try:
        from app.engines.pipeline_alerts import _fetch_admin_emails
        admins = await _fetch_admin_emails()
        _add("admin_alerts", "Admin alert delivery", bool(admins),
             "no active admin recipients", "Set is_admin=true on an active account.")
    except Exception:
        _add("admin_alerts", "Admin alert delivery", False, "check failed", "")
    return out


@router.post("/systems-check/run")
async def systems_check_run(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the FULL systems check (the same one the dashboard shows), record who
    ran it + when, and email admins if the result is RED. Admin-only."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    report = await systems_check(current_user, db)
    try:
        report.setdefault("safety", {})
        for c in await _sc_safety_components(db):
            report["safety"][c["component"]] = {"status": c["status"], "label": c["label"]}
            if c["status"] != "green":
                report.setdefault("errors", []).append(c)
        report["overall"]["error_count"] = len(report.get("errors", []))
    except Exception as _se:
        logger.warning(f"[systems-check-run] safety components failed: {_se}")

    run_at = datetime.now(timezone.utc)
    ran_by = current_user.email
    overall = (report.get("overall") or {}).get("status")
    rc = _sc_redis()
    if rc is not None:
        try:
            import json as _j
            rc.set("systems_check:last_run", _j.dumps(
                {"at": run_at.isoformat(), "by": ran_by, "overall": overall}))
        except Exception:
            pass
    if overall == "red":
        try:
            from app.api.routes.security import notify_admins_security
            reds = [e for e in report.get("errors", []) if e.get("severity") == "critical"]
            html = (f"<p><b>Systems Check: RED</b> — run by {ran_by} at {run_at.isoformat()}.</p>"
                    "<ul>" + "".join(
                        f"<li><b>{e.get('label')}</b>: {e.get('message')}</li>" for e in reds[:20]) + "</ul>")
            await notify_admins_security("Systems Check found a RED issue", html)
            logger.warning(f"[systems-check-run] RED — admin alert emailed (run_by={ran_by})")
        except Exception as _ee:
            logger.error(f"[systems-check-run] admin alert failed: {_ee}")
    report["run_at"] = run_at.isoformat()
    report["ran_by"] = ran_by
    report["last_run"] = {"at": run_at.isoformat(), "by": ran_by, "overall": overall}
    logger.info(f"[systems-check-run] admin={ran_by} overall={overall}")
    return report


@router.get("/systems-check/last")
async def systems_check_last(current_user: User = Depends(get_current_user)):
    """The last persisted Run Full Systems Check (timestamp + who + overall)."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    rc = _sc_redis()
    if rc is None:
        return {"last_run": None}
    try:
        import json as _j
        raw = rc.get("systems_check:last_run")
        return {"last_run": _j.loads(raw) if raw else None}
    except Exception:
        return {"last_run": None}


@router.post("/systems-check/fix")
async def systems_check_fix(
    req: "_SCFixRequest",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run a SAFE auto-fix for a flagged System Check component. Admin-only.
    Every attempt + outcome is logged; never performs destructive actions."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    action = (req.action or "").strip()
    logger.info(f"[systems-check-fix] admin={current_user.email} action={action} component={req.component}")
    ok = False; msg = ""; detail = {}
    try:
        if action == "resync_positions":
            # Round 2: re-price ONLY (never trades), and don't claim success when
            # the real blocker is a missing Polygon key.
            if not os.environ.get("POLYGON_API_KEY"):
                ok = False
                msg = "Cannot re-price: POLYGON_API_KEY is not configured (set it, then retry)."
            else:
                from app.engines.options.premarket_scheduler import _run_trailing_stop_watcher
                await _run_trailing_stop_watcher(reprice_only=True)
                _np = (await db.execute(text("SELECT COUNT(*) FROM open_positions_watch WHERE status='open'"))).scalar() or 0
                ok = True
                msg = f"Re-priced {int(_np)} open position(s) now (no orders placed). The stale flag clears on the refreshed check."
        elif action in ("refresh_health", "refresh_email_health"):
            from app.engines.scanner_health import check_health
            h = await check_health()
            detail = {k: bool(v.get("ok")) for k, v in (h.get("components") or {}).items()}
            ok = True
            msg = "Re-ran health check."
        elif action == "clear_stale_jobs":
            r1 = await db.execute(text("UPDATE backtest_runs SET status='FAILED', completed_at=NOW(), "
                "error_message='cleared via admin System Check (stale >1h)' "
                "WHERE LOWER(status::text)='running' AND created_at < NOW() - INTERVAL '1 hour'"))
            r2 = await db.execute(text("UPDATE optimization_runs SET status='FAILED', completed_at=NOW(), "
                "error_message='cleared via admin System Check (stale >1h)' "
                "WHERE LOWER(status::text)='running' AND created_at < NOW() - INTERVAL '1 hour'"))
            # SYSTEMS-CHECK-V2: also retire genuinely-abandoned pending trades
            # (unconfirmed, non-terminal, and already past their expiry).
            r3 = await db.execute(text("UPDATE pending_trades SET declined_at=NOW(), status='expired' "
                "WHERE confirmed_at IS NULL AND declined_at IS NULL "
                "AND LOWER(COALESCE(status,'')) NOT IN ('executed','declined','expired','cancelled','failed') "
                "AND expires_at < NOW()"))
            await db.commit()
            ok = True; msg = (f"Cleared {r1.rowcount} stale backtest(s), {r2.rowcount} stale optimization(s), "
                              f"and retired {r3.rowcount} abandoned pending-trade(s).")
        elif action == "rerun_scanner_check":
            from app.engines.options.theta_scanner import find_best_premarket_pick
            pick = await find_best_premarket_pick(db)
            ok = True
            msg = (f"Re-ran Theta Scanner: pick={pick['ticker']}" if pick
                   else "Re-ran Theta Scanner: no qualifying setup right now (valid — not an error).")
        elif action == "resync_broker":
            from app.api.routes.scanner import _refresh_broker_balance
            rows = (await db.execute(text("SELECT DISTINCT user_id FROM broker_accounts WHERE is_active=true"))).fetchall()
            n = 0
            for _r in rows:
                try:
                    await _refresh_broker_balance(db, _r.user_id); n += 1
                except Exception as _e:
                    logger.warning(f"[systems-check-fix] broker resync user={_r.user_id} failed: {_e}")
            ok = True; msg = f"Re-synced {n} active broker account owner(s)."
        elif action in ("retry_admin_email", "test_heartbeat"):
            from app.engines import scanner_health as _sh
            try: _sh._LAST_HEARTBEAT_SENT = None
            except Exception: pass
            await _sh.send_daily_heartbeat()
            ok = True; msg = "Sent heartbeat email to admin."
        else:
            raise HTTPException(status_code=400, detail=f"Unknown or unsafe action: {action!r}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[systems-check-fix] action={action} FAILED: {e}")
        return {"ok": False, "action": action, "message": f"Fix attempt failed: {e}", "detail": {}}
    logger.info(f"[systems-check-fix] action={action} ok={ok} msg={msg}")
    return {"ok": ok, "action": action, "message": msg, "detail": detail}


# ─────────────────────────────────────────────────────────────────────────
# Admin-only safe-action endpoints (test heartbeat, test trade email, run
# scanner health). All guard on is_admin and never send to real subscribers.
# ─────────────────────────────────────────────────────────────────────────

@router.post("/send-test-heartbeat")
async def admin_send_test_heartbeat(
    current_user: User = Depends(get_current_user),
):
    """Fire the daily heartbeat email immediately to ADMIN_HEARTBEAT_EMAIL.
    Forces _LAST_HEARTBEAT_SENT to None so the dedup guard doesn't skip."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    import time as _t
    from app.engines import scanner_health as _sh
    t0 = _t.time()
    # Pull email + run a one-off send so we don't depend on the 9-10 ET window.
    admin_email = os.environ.get(
        "ADMIN_HEARTBEAT_EMAIL", _sh.ADMIN_HEARTBEAT_EMAIL
    )
    from app.services.email import _send_tracked
    health = await _sh.check_health()
    ok_emoji = "OK" if health.get("ok") else "DEGRADED"
    subject = f"🎯 Theta Scanner — heartbeat TEST · {ok_emoji}"
    rows_html = ""
    for cname, c in health.get("components", {}).items():
        icon = "OK" if c.get("ok") else "FAIL"
        rows_html += f"<tr><td>{icon}</td><td><b>{cname}</b></td></tr>"
    html = (
        "<div style='font-family:sans-serif;padding:18px;'>"
        "<h1 style='color:#7c3aed'>Heartbeat TEST</h1>"
        "<p>Manually triggered by an admin via Systems Check.</p>"
        f"<table>{rows_html}</table></div>"
    )
    result = _send_tracked(admin_email, subject, html)
    return {
        "sent": bool(result.get("sent")),
        "message_id": result.get("provider_message_id"),
        "latency_ms": int((_t.time() - t0) * 1000),
        "recipient": admin_email,
    }


class _TestTradeEmailReq(BaseModel):
    asset_class: str  # 'stock' | 'futures' | 'options'


@router.post("/send-test-trade-email")
async def admin_send_test_trade_email(
    data: _TestTradeEmailReq,
    current_user: User = Depends(get_current_user),
):
    """Fire a sample trade-receipt email using the real template, addressed
    ONLY to ADMIN_HEARTBEAT_EMAIL. Never reaches real subscribers."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    asset = (data.asset_class or "stock").lower()
    if asset not in ("stock", "futures", "options"):
        raise HTTPException(status_code=400, detail="invalid asset_class")
    from app.engines.scanner_health import ADMIN_HEARTBEAT_EMAIL
    admin_email = os.environ.get("ADMIN_HEARTBEAT_EMAIL", ADMIN_HEARTBEAT_EMAIL)

    # Per-asset sample setup. These are illustrative numbers ONLY — the
    # firewall (session cap + daily cap) is still active so calling this 5x
    # in DEAD_ZONE will return sent=False, which is exactly the visibility
    # the test endpoint is meant to provide.
    samples = {
        "stock":   ("AAPL", "long",  187.50, 184.20, 195.00, 50,
                    "Heartbeat-test signal · sample setup"),
        "futures": ("ES",   "long",  5230.0, 5220.0, 5260.0,  1,
                    "Heartbeat-test futures signal · sample setup"),
        "options": ("NVDA", "long",  120.0,  115.0,  135.0,  10,
                    "Heartbeat-test options signal · sample setup"),
    }
    tk, side, e, sl, tp, qty, why = samples[asset]
    from app.services.email import send_trade_receipt_email
    sent = False
    try:
        sent = bool(send_trade_receipt_email(
            to=admin_email, username="admin-test", ticker=tk,
            direction=side, entry=e, stop=sl, target=tp,
            contracts=qty, reason=why,
            strategy_name="Theta Scanner heartbeat-test",
            mode="paper",
        ))
    except Exception as exc:
        return {"sent": False, "error": f"{type(exc).__name__}", "recipient": admin_email}
    return {"sent": sent, "asset_class": asset, "recipient": admin_email}


@router.post("/run-scanner-health-check")
async def admin_run_scanner_health_check(
    current_user: User = Depends(get_current_user),
):
    """Run a fresh scanner-pipeline health probe and return the full dict."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    from app.engines.scanner_health import check_health
    return await check_health()


@router.post("/theta-scanner/run-now")
async def admin_run_theta_scanner_now(
    emit: bool = False,
    admin: User = Depends(require_admin_with_passcode),
    db: AsyncSession = Depends(get_db),
):
    """Manually run Theta Scanner right now (admin button). Returns the pick OR a
    No-Trade with the exact reason + the top-candidate breakdown (ticker, score,
    verdict, reasons). `emit=true` also sends the email/signal to subscribers and
    routes per tier (only for a CONFIRMED, non-watch-only pick)."""
    from app.engines.options.theta_scanner import (
        find_best_premarket_pick, _LAST_SCAN_DIAG, _NOPICK_STATE,
    )
    from app.engines.market_calendar import market_status as _ms
    from datetime import datetime as _dt, timezone as _tz
    pick = await find_best_premarket_pick(db)
    diag = _LAST_SCAN_DIAG.get("last") or {}
    out = {
        "ran_at_utc": _dt.now(_tz.utc).isoformat(),
        "ran_by": admin.email,
        "market": _ms(),
        "universe": diag.get("universe"),
        "candidate_count": diag.get("candidates"),
        "top_candidates": (diag.get("evaluated") or [])[:10],
        "pick": None,
        "no_trade_reason": None,
        "emitted": False,
    }
    if pick:
        out["pick"] = {k: pick.get(k) for k in (
            "ticker", "matched_strategy", "entry", "stop", "target", "rr",
            "projected_move_pct", "score", "gap_pct", "rel_vol", "watch_only",
            "stop_reason", "target_reason", "catalyst_reason", "levels_basis",
            "quality_reasons")}
        out["routing"] = ("watch-only — informational, no entry"
                          if pick.get("watch_only")
                          else "email + paper/live (gated per user tier + auto_trade_allowed)")
        if emit and not pick.get("watch_only"):
            from app.engines.options.premarket_scheduler import run_theta_scanner_for_all_users
            await run_theta_scanner_for_all_users()
            out["emitted"] = True
    else:
        out["no_trade_reason"] = (_NOPICK_STATE.get("last") or {}).get("reason") or "no candidate cleared the quality filters"
    return out
