"""Email-code verification + security audit — the shared foundation for
risk-increasing changes AND fully-automated trading activation.

Flow:
  1. POST /security/verify-code/request  {purpose, context?}  -> emails a 6-digit
     code (hashed at rest, TTL 10m), audits 'verify_code_sent'.
  2. POST /security/verify-code/confirm  {purpose, code}      -> consumes the
     code on match (attempt-limited), audits 'verify_code_confirmed/failed'.
  3. The sensitive action endpoint calls require_recent_verification(purpose),
     which passes only if the user CONSUMED a code for that purpose within the
     recent window — then it records the change + notifies admins.
"""
import uuid
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.core.auth import get_current_user
from app.services import email as email_svc

router = APIRouter()

CODE_TTL_MIN = 10          # a code is valid for this many minutes
RECENT_WINDOW_MIN = 10     # a consumed code authorizes the action for this long
MAX_ATTEMPTS = 5           # failed confirms before a code is locked
RESEND_COOLDOWN_SEC = 30   # min seconds between code requests for the same purpose

# Audit event types (written to security_audit_log).
EVENT_CODE_SENT = "verify_code_sent"
EVENT_CODE_CONFIRMED = "verify_code_confirmed"
EVENT_CODE_FAILED = "verify_code_failed"
EVENT_AUTOMATION_ENABLED = "automation_enabled"
EVENT_AUTOMATION_DISABLED = "automation_disabled"
EVENT_RISK_CHANGE = "risk_change"
EVENT_TRADE_APPROVED = "trade_approved"
EVENT_TRADE_DECLINED = "trade_declined"
EVENT_AUTO_TRADE_BLOCKED = "auto_trade_blocked"
EVENT_AGREEMENT_ACCEPTED = "agreement_accepted"

# Human labels for the email body, per purpose.
PURPOSE_LABELS = {
    "enable_automation": "enable fully automated trading",
    "risk_change": "change your risk settings",
}


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _gen_code() -> str:
    return f"{secrets.randbelow(900000) + 100000:06d}"   # cryptographically-random 6-digit


def _client(request: Optional[Request]):
    if request is None:
        return None, None
    ip = request.client.host if request.client else None
    ua = request.headers.get("user-agent")
    return ip, ua


async def audit_log(db: AsyncSession, user_id, event_type: str,
                    detail: Optional[dict] = None, request: Optional[Request] = None) -> None:
    """Append a row to the security audit trail. Best-effort — never raises into
    the caller (audit failure must not break a security action's primary path)."""
    import json
    ip, ua = _client(request)
    try:
        await db.execute(text("""
            INSERT INTO security_audit_log (id, user_id, event_type, detail, ip_address, user_agent, created_at)
            VALUES (:id, :uid, :ev, CAST(:detail AS JSONB), :ip, :ua, :now)
        """), {
            "id": str(uuid.uuid4()), "uid": str(user_id) if user_id else None,
            "ev": event_type, "detail": json.dumps(detail or {}),
            "ip": ip, "ua": ua, "now": datetime.now(timezone.utc),
        })
    except Exception:
        # leave commit to the caller; swallow audit errors
        pass


async def notify_admins_security(subject: str, html: str) -> None:
    """Email all admins about a sensitive security/trading event. Best-effort."""
    try:
        from app.engines.pipeline_alerts import _fetch_admin_emails
        admins = await _fetch_admin_emails()
        for a in admins or []:
            try:
                email_svc._send(a, f"[Admin] {subject}", html)
            except Exception:
                continue
    except Exception:
        pass


async def request_verification_code(db: AsyncSession, user: User, purpose: str,
                                     context: Optional[dict], request: Optional[Request]) -> dict:
    """Generate + email a 6-digit code for `purpose`. Rate-limited per purpose."""
    now = datetime.now(timezone.utc)
    # Resend cooldown — don't let a client spam codes.
    r = await db.execute(text("""
        SELECT created_at FROM verification_codes
         WHERE user_id = :uid AND purpose = :p
         ORDER BY created_at DESC LIMIT 1
    """), {"uid": str(user.id), "p": purpose})
    row = r.fetchone()
    if row and row.created_at and (now - row.created_at).total_seconds() < RESEND_COOLDOWN_SEC:
        wait = int(RESEND_COOLDOWN_SEC - (now - row.created_at).total_seconds())
        raise HTTPException(status_code=429, detail=f"Please wait {wait}s before requesting another code.")

    code = _gen_code()
    import json
    ip, ua = _client(request)
    await db.execute(text("""
        INSERT INTO verification_codes
            (id, user_id, purpose, code_hash, context, created_at, expires_at, attempts, ip_address, user_agent)
        VALUES (:id, :uid, :p, :h, CAST(:ctx AS JSONB), :now, :exp, 0, :ip, :ua)
    """), {
        "id": str(uuid.uuid4()), "uid": str(user.id), "p": purpose,
        "h": _hash_code(code), "ctx": json.dumps(context or {}),
        "now": now, "exp": now + timedelta(minutes=CODE_TTL_MIN), "ip": ip, "ua": ua,
    })
    await audit_log(db, user.id, EVENT_CODE_SENT, {"purpose": purpose}, request)
    await db.commit()

    label = PURPOSE_LABELS.get(purpose, "confirm a sensitive change")
    try:
        email_svc.send_verification_code_email(
            to=user.email, username=(user.username or "trader"), code=code,
            purpose_label=label, ttl_min=CODE_TTL_MIN,
        )
    except Exception:
        # The code is stored; surface a soft error so the user can retry.
        raise HTTPException(status_code=502, detail="Could not send the verification email; please retry.")
    return {"sent": True, "purpose": purpose, "expires_in_min": CODE_TTL_MIN}


async def confirm_verification_code(db: AsyncSession, user: User, purpose: str,
                                    code: str, request: Optional[Request]) -> bool:
    """Verify a submitted code for `purpose`. Consumes it on success."""
    now = datetime.now(timezone.utc)
    r = await db.execute(text("""
        SELECT id, code_hash, attempts FROM verification_codes
         WHERE user_id = :uid AND purpose = :p AND consumed_at IS NULL AND expires_at > :now
         ORDER BY created_at DESC LIMIT 1
    """), {"uid": str(user.id), "p": purpose, "now": now})
    row = r.fetchone()
    if not row:
        await audit_log(db, user.id, EVENT_CODE_FAILED, {"purpose": purpose, "reason": "no_active_code"}, request)
        await db.commit()
        raise HTTPException(status_code=400, detail="No active code — request a new one.")
    if row.attempts >= MAX_ATTEMPTS:
        await audit_log(db, user.id, EVENT_CODE_FAILED, {"purpose": purpose, "reason": "locked"}, request)
        await db.commit()
        raise HTTPException(status_code=429, detail="Too many attempts — request a new code.")
    if _hash_code((code or "").strip()) != row.code_hash:
        await db.execute(text("UPDATE verification_codes SET attempts = attempts + 1 WHERE id = :id"),
                         {"id": str(row.id)})
        await audit_log(db, user.id, EVENT_CODE_FAILED, {"purpose": purpose, "reason": "mismatch"}, request)
        await db.commit()
        raise HTTPException(status_code=400, detail="Incorrect code.")
    await db.execute(text("UPDATE verification_codes SET consumed_at = :now WHERE id = :id"),
                     {"now": now, "id": str(row.id)})
    await audit_log(db, user.id, EVENT_CODE_CONFIRMED, {"purpose": purpose}, request)
    await db.commit()
    return True


async def require_recent_verification(db: AsyncSession, user_id, purpose: str,
                                      within_minutes: int = RECENT_WINDOW_MIN) -> None:
    """Raise 403 unless the user CONSUMED a code for `purpose` within the window.
    Sensitive-action endpoints (automation activation, risk-increasing changes)
    call this AFTER the 'I agree' acknowledgment and BEFORE applying the change."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=within_minutes)
    r = await db.execute(text("""
        SELECT 1 FROM verification_codes
         WHERE user_id = :uid AND purpose = :p AND consumed_at IS NOT NULL AND consumed_at >= :cut
         ORDER BY consumed_at DESC LIMIT 1
    """), {"uid": str(user_id), "p": purpose, "cut": cutoff})
    if r.fetchone() is None:
        raise HTTPException(status_code=403, detail=f"verification_required:{purpose}")


# ── Endpoints ────────────────────────────────────────────────────────────────

class CodeRequest(BaseModel):
    purpose: str
    context: Optional[dict] = None


class CodeConfirm(BaseModel):
    purpose: str
    code: str


@router.post("/verify-code/request")
async def post_request_code(data: CodeRequest, request: Request,
                            current_user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    if data.purpose not in PURPOSE_LABELS:
        raise HTTPException(status_code=400, detail="Unknown verification purpose.")
    return await request_verification_code(db, current_user, data.purpose, data.context, request)


@router.post("/verify-code/confirm")
async def post_confirm_code(data: CodeConfirm, request: Request,
                            current_user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    await confirm_verification_code(db, current_user, data.purpose, data.code, request)
    return {"verified": True, "purpose": data.purpose, "valid_for_min": RECENT_WINDOW_MIN}


@router.get("/audit")
async def get_my_audit(current_user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    """The signed-in user's own recent security events."""
    r = await db.execute(text("""
        SELECT event_type, detail, ip_address, created_at
          FROM security_audit_log WHERE user_id = :uid
         ORDER BY created_at DESC LIMIT 100
    """), {"uid": str(current_user.id)})
    return [{"event_type": x.event_type, "detail": x.detail,
             "ip_address": x.ip_address,
             "created_at": x.created_at.isoformat() if x.created_at else None}
            for x in r.fetchall()]


@router.get("/audit-summary")
async def audit_summary(current_user: User = Depends(get_current_user),
                        db: AsyncSession = Depends(get_db)):
    """Admin (tier_5): consolidated security/trading audit — counts by event type
    over 30 days + the most recent events. Covers agreement acceptance, automation
    enable/disable, risk-increasing changes, trade approvals/declines, blocked
    auto-trade attempts, and signal routing."""
    tier = current_user.subscription_tier.value if hasattr(current_user.subscription_tier, "value") else str(current_user.subscription_tier)
    if tier != "tier_5":
        raise HTTPException(status_code=403, detail="Admin only.")
    by_type = (await db.execute(text("""
        SELECT event_type, count(*) AS n, max(created_at) AS last_at
          FROM security_audit_log WHERE created_at > now() - interval '30 days'
         GROUP BY 1 ORDER BY 2 DESC
    """))).fetchall()
    recent = (await db.execute(text("""
        SELECT event_type, user_id, detail, ip_address, created_at
          FROM security_audit_log ORDER BY created_at DESC LIMIT 100
    """))).fetchall()
    return {
        "by_event_type": [{"event_type": r.event_type, "count": int(r.n),
                           "last_at": r.last_at.isoformat() if r.last_at else None} for r in by_type],
        "recent": [{"event_type": r.event_type, "user_id": str(r.user_id) if r.user_id else None,
                    "detail": r.detail, "ip_address": r.ip_address,
                    "created_at": r.created_at.isoformat() if r.created_at else None} for r in recent],
    }

