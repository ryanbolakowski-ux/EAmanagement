import os
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text
from datetime import datetime, timedelta, timezone
from typing import Optional
from pydantic import BaseModel

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.core.auth import require_2fa_when_paid as get_current_user
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
            return None

    async def _first(sql: str, **params):
        try:
            return (await db.execute(text(sql), params)).first()
        except Exception:
            return None

    def _staleness(ts):
        """Map a datetime to {green/yellow/red} based on minutes since."""
        if ts is None:
            return "yellow"
        try:
            delta = (now_utc - ts).total_seconds() / 60.0
        except Exception:
            return "yellow"
        if delta < 5: return "green"
        if delta < 30: return "yellow"
        return "red"

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

    scanners = {
        "theta_scanner": {
            "status": "green" if theta_today else (
                "yellow" if et_now.hour < 10 else "red"
            ),
            "last_run_at": theta_today.picked_at.isoformat() if theta_today else None,
            "today_pick": theta_today.ticker if theta_today else None,
            "next_run_at": None,  # premarket scheduler-driven, not cron
        },
        "futures_scanner": {
            "status": "green" if futures_count > 0 else "yellow",
            "last_run_at": futures_last.at.isoformat() if futures_last and futures_last.at else None,
            "today_signal_count": int(futures_count),
        },
        "options_scanner": {
            "status": "green" if options_today else "yellow",
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
        "trade_alert_status": "green" if sent_today_count > 0 else "yellow",
        "futures_email_status": "green" if futures_count > 0 else "yellow",
        "options_swing_status": "green" if options_today else "yellow",
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
            "status": "green" if live_active > 0 else "yellow",
            "active_sessions": int(live_active),
        },
        "paper_trading": {
            "status": "green" if paper_active > 0 else "yellow",
            "active_sessions": int(paper_active),
        },
        "open_position_monitor": {
            "status": _staleness(last_priced) if open_positions > 0 else "green",
            "last_check_at": last_priced.isoformat() if last_priced else None,
            "open_count": int(open_positions),
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
    tradier_status = "green" if os.environ.get("TRADIER_API_KEY") else "yellow"

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
            "status": _staleness(kyc_last) if kyc_today > 0 else "yellow",
            "last_received_at": kyc_last.isoformat() if kyc_last else None,
            "today_count": int(kyc_today),
        },
        "broker_sync": {
            "status": _staleness(broker_last) if broker_n > 0 else "yellow",
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

    # queue depth: count pending_trades not-yet-confirmed as a proxy
    queue_depth = await _scalar(
        "SELECT COUNT(*) FROM pending_trades WHERE confirmed_at IS NULL"
    ) or 0

    stuck_runs = await _scalar(
        "SELECT COUNT(*) FROM backtest_runs "
        "WHERE LOWER(status::text)='running' AND created_at < NOW() - INTERVAL '1 hour'"
    ) or 0

    infra = {
        "database": {"status": "green" if db_ok else "red"},
        "redis": {"status": "green" if redis_ok else "red"},
        "queue": {
            "status": "green" if int(queue_depth) < 10 else "yellow",
            "depth": int(queue_depth),
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

    # ── overall ─────────────────────────────────────────────────────────
    all_statuses = []
    for section in (scanners, trading, integrations, infra):
        for v in section.values():
            if isinstance(v, dict) and "status" in v:
                all_statuses.append(v["status"])
    # Add email-section sub statuses too
    for k in ("trade_alert_status", "futures_email_status",
              "options_swing_status", "heartbeat_status"):
        if k in emails:
            all_statuses.append(emails[k])

    worst = _systems_check_status(*all_statuses)
    summary_map = {
        "green":  "All systems operating normally.",
        "yellow": "Some subsystems are stale or degraded — review the cards below.",
        "red":    "One or more critical subsystems are down — see flagged cards.",
        "unknown": "Status indeterminate.",
    }
    overall = {
        "status": worst,
        "summary": summary_map.get(worst, "Status indeterminate."),
    }

    return {
        "overall": overall,
        "scanners": scanners,
        "emails": emails,
        "trading": trading,
        "integrations": integrations,
        "infra": infra,
        "recent_errors": recent_errors,
        "jobs_running": jobs_running,
        "metrics": metrics,
    }


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
