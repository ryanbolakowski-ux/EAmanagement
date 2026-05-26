"""KYC routes — Stripe Identity scaffold.

Status values: 'not_started' | 'pending' | 'verified' | 'failed' | 'requires_input'

Flow:
  1. User clicks "Verify identity" → POST /kyc/start
  2. Backend creates a Stripe Identity VerificationSession
  3. Frontend opens the Stripe-hosted ID + selfie capture flow
  4. Stripe webhook hits /kyc/webhook on completion
  5. On 'verified': set users.kyc_status='verified', kyc_verified_at=NOW()
  6. All live-trading endpoints require kyc_status='verified' (require_kyc gate)
"""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text, select
from loguru import logger

from app.database import get_db
from app.core.auth import get_current_user
from app.models.user import User

router = APIRouter()

# --- kyc audit log helper ---
_KYC_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS kyc_events (
    id          BIGSERIAL PRIMARY KEY,
    user_id     UUID NOT NULL,
    user_email  TEXT,
    event_type  TEXT NOT NULL,
    status      TEXT,
    provider    TEXT,
    session_id  TEXT,
    country     TEXT,
    ip          TEXT,
    detail      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_kyc_events_created ON kyc_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kyc_events_user    ON kyc_events (user_id, created_at DESC);
"""

_kyc_table_checked = False
async def _ensure_kyc_events_table(db):
    global _kyc_table_checked
    if _kyc_table_checked: return
    try:
        await db.execute(text(_KYC_EVENTS_DDL))
        await db.commit()
        _kyc_table_checked = True
    except Exception as e:
        logger.warning(f"[kyc] could not ensure kyc_events table: {e}")

async def _log_kyc_event(db, *, user_id: str, user_email: str | None,
                          event_type: str, status: str | None = None,
                          provider: str | None = None, session_id: str | None = None,
                          country: str | None = None, ip: str | None = None,
                          detail: str | None = None):
    """Insert one row into kyc_events. Best-effort — failures logged not raised."""
    try:
        await _ensure_kyc_events_table(db)
        await db.execute(text("""
            INSERT INTO kyc_events
                (user_id, user_email, event_type, status, provider,
                 session_id, country, ip, detail)
            VALUES (:uid, :em, :et, :st, :pr, :sid, :ct, :ip, :dt)
        """), {
            "uid": user_id, "em": user_email, "et": event_type, "st": status,
            "pr": provider, "sid": session_id, "ct": country, "ip": ip, "dt": detail,
        })
        await db.commit()
    except Exception as e:
        logger.warning(f"[kyc] audit-log insert failed: {e}")
# --- end kyc audit log helper ---


STRIPE_IDENTITY_KEY = os.environ.get("STRIPE_IDENTITY_KEY") or os.environ.get("STRIPE_SECRET_KEY", "")
# Minimum age for US derivatives / securities trading.
MIN_AGE = 18


def _age_from_dob(dob_str: str) -> int | None:
    """Return age in whole years from YYYY-MM-DD, or None if unparseable."""
    try:
        from datetime import date
        y, m, d = dob_str.split("-")
        born = date(int(y), int(m), int(d))
        today = date.today()
        return today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    except Exception:
        return None



class KycStartRequest(BaseModel):
    first_name: str
    last_name: str
    date_of_birth: str  # YYYY-MM-DD
    country_code: str = "US"


@router.get("/status")
async def kyc_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the user's KYC verification status."""
    return {
        "status": getattr(current_user, "kyc_status", None) or "not_started",
        "verified_at": current_user.kyc_verified_at.isoformat() if getattr(current_user, "kyc_verified_at", None) else None,
        "provider": getattr(current_user, "kyc_provider", None),
        "country": getattr(current_user, "country_code", None),
    }


@router.post("/start", status_code=status.HTTP_201_CREATED)
async def kyc_start(
    data: KycStartRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Begin a KYC verification. Creates a Stripe Identity session and
    returns a client_secret + url for the frontend to open."""
    if data.country_code.upper() not in ("US",):
        raise HTTPException(status_code=400, detail="Theta Algos is only available to US residents.")

    # Hard 18+ gate — US derivatives + securities require 18+ across all states.
    age = _age_from_dob(data.date_of_birth)
    if age is None:
        raise HTTPException(status_code=400, detail="Invalid date of birth. Use YYYY-MM-DD.")
    if age < MIN_AGE:
        # Log the rejection so admins can see under-age attempts
        try:
            await _log_kyc_event(db, user_id=str(current_user.id), user_email=current_user.email,
                event_type="rejected_underage", status="failed", provider="age_check",
                country=data.country_code.upper(),
                detail=f"DOB {data.date_of_birth} -> age {age}, below MIN_AGE={MIN_AGE}")
        except Exception:
            pass
        raise HTTPException(
            status_code=403,
            detail=f"You must be at least {MIN_AGE} years old to use Theta Algos. US derivatives and securities regulations prohibit minors from trading on this platform.",
        )


    if not STRIPE_IDENTITY_KEY:
        # Stub mode — no Stripe key set, mark as pending so the UI shows it
        await db.execute(text("""
            UPDATE users SET
              kyc_status = 'pending',
              kyc_provider = 'stub',
              first_name = :fn, last_name = :ln,
              date_of_birth = :dob, country_code = :cc
            WHERE id = :uid
        """), {
            "fn": data.first_name, "ln": data.last_name,
            "dob": data.date_of_birth, "cc": data.country_code.upper(),
            "uid": str(current_user.id),
        })
        await db.commit()
        await _log_kyc_event(db, user_id=str(current_user.id), user_email=current_user.email,
            event_type="started", status="pending", provider="stub",
            country=data.country_code.upper(),
            ip=(request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for","").split(",")[0].strip() or "") if hasattr(request, "headers") else None,
            detail="STRIPE_IDENTITY_KEY not configured")
        return {
            "status": "stub",
            "message": "STRIPE_IDENTITY_KEY not configured — KYC stubbed as pending. Set the env var to enable real verification.",
            "redirect_url": None,
            "client_secret": None,
        }

    # Real Stripe Identity path
    try:
        import stripe
        stripe.api_key = STRIPE_IDENTITY_KEY
        session = stripe.identity.VerificationSession.create(
            type="document",
            options={"document": {"require_matching_selfie": True}},
            metadata={"user_id": str(current_user.id), "email": current_user.email},
        )
        await db.execute(text("""
            UPDATE users SET
              kyc_status = 'pending',
              kyc_provider = 'stripe_identity',
              kyc_session_id = :sid,
              first_name = :fn, last_name = :ln,
              date_of_birth = :dob, country_code = :cc
            WHERE id = :uid
        """), {
            "sid": session.id,
            "fn": data.first_name, "ln": data.last_name,
            "dob": data.date_of_birth, "cc": data.country_code.upper(),
            "uid": str(current_user.id),
        })
        await db.commit()
        await _log_kyc_event(db, user_id=str(current_user.id), user_email=current_user.email,
            event_type="started", status="pending", provider="stripe_identity",
            session_id=session.id, country=data.country_code.upper())
        return {
            "status": "pending",
            "session_id": session.id,
            "client_secret": session.client_secret,
            "redirect_url": session.url,
        }
    except Exception as e:
        logger.error(f"[kyc] Stripe Identity create failed: {e}")
        msg = str(e)
        # Specific Stripe error: Identity API not yet approved for this account's
        # use case (common for KYC on trading platforms — Stripe reviews manually).
        # Fall back to manual-review mode so onboarding isn't blocked. Record the
        # user's submitted details + mark as 'manual_review' for admin to approve.
        if "identity_api_invalid_application" in msg or "Stripe Identity supported use-cases" in msg:
            await db.execute(text("""
                UPDATE users SET
                  kyc_status = 'manual_review',
                  kyc_provider = 'manual',
                  first_name = :fn, last_name = :ln,
                  date_of_birth = :dob, country_code = :cc
                WHERE id = :uid
            """), {
                "fn": data.first_name, "ln": data.last_name,
                "dob": data.date_of_birth, "cc": data.country_code.upper(),
                "uid": str(current_user.id),
            })
            await db.commit()
            await _log_kyc_event(db, user_id=str(current_user.id), user_email=current_user.email,
                event_type="manual_review_queued", status="manual_review",
                provider="manual", session_id=None, country=data.country_code.upper())
            return {
                "status": "manual_review",
                "message": ("Your information has been submitted. Our team will review "
                            "and approve your identity within 1 business day. You can "
                            "continue using paper-trading and backtests in the meantime; "
                            "live broker connectivity will unlock after approval."),
                "redirect_url": None,
                "client_secret": None,
            }
        raise HTTPException(status_code=502, detail=f"KYC provider error: {e}")


@router.post("/webhook")
async def kyc_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    """Stripe Identity webhook receiver. Verify signature, then update user."""
    if not STRIPE_IDENTITY_KEY:
        return {"status": "stub"}
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_IDENTITY_WEBHOOK_SECRET", "")
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured.")
    try:
        import stripe
        event = stripe.Webhook.construct_event(body, sig, webhook_secret)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature.")

    obj = event["data"]["object"]
    session_id = obj.get("id")
    user_id = (obj.get("metadata") or {}).get("user_id")
    event_type = event.get("type")

    if event_type == "identity.verification_session.verified":
        # Cross-check: the document's country MUST be US.
        # Stripe returns verified_outputs.address.country once the ID has been validated.
        doc_country = None
        try:
            verified = obj.get("verified_outputs") or {}
            doc_country = ((verified.get("address") or {}).get("country") or "").upper() or None
            # Stripe sometimes returns the issuing-country in dob document fields instead
            if not doc_country:
                doc_country = ((verified.get("id_number") or {}).get("country") or "").upper() or None
        except Exception:
            doc_country = None

        if doc_country and doc_country != "US":
            # User passed Stripe verification but the document is from a different country.
            # Mark as failed + log it loudly.
            await db.execute(text(
                "UPDATE users SET kyc_status='failed' WHERE id = :uid AND kyc_session_id = :sid"
            ), {"uid": user_id, "sid": session_id})
            await db.commit()
            try:
                await _log_kyc_event(db, user_id=user_id or "", user_email=None,
                    event_type="webhook", status="failed", provider="stripe_identity",
                    session_id=session_id, country=doc_country,
                    detail=f"Stripe verified user but document country is {doc_country}, not US — rejected")
            except Exception: pass
            logger.warning(f"[kyc] REJECTED user={user_id} session={session_id} doc_country={doc_country} (non-US)")
        else:
            await db.execute(text(
                "UPDATE users SET kyc_status='verified', kyc_verified_at=NOW(), country_code='US' "
                "WHERE id = :uid AND kyc_session_id = :sid"
            ), {"uid": user_id, "sid": session_id})
            await db.commit()
            logger.info(f"[kyc] verified user={user_id} session={session_id} doc_country={doc_country or 'unknown'}")
    elif event_type == "identity.verification_session.requires_input":
        await db.execute(text(
            "UPDATE users SET kyc_status='requires_input' "
            "WHERE id = :uid AND kyc_session_id = :sid"
        ), {"uid": user_id, "sid": session_id})
        await db.commit()
    elif event_type == "identity.verification_session.canceled":
        await db.execute(text(
            "UPDATE users SET kyc_status='failed' "
            "WHERE id = :uid AND kyc_session_id = :sid"
        ), {"uid": user_id, "sid": session_id})
        await db.commit()

    # Audit log every webhook event we recognized
    if event_type in ("identity.verification_session.verified",
                       "identity.verification_session.requires_input",
                       "identity.verification_session.canceled"):
        status_label = {"identity.verification_session.verified": "verified",
                        "identity.verification_session.requires_input": "requires_input",
                        "identity.verification_session.canceled": "failed"}[event_type]
        await _log_kyc_event(db, user_id=user_id or "", user_email=None,
            event_type="webhook", status=status_label, provider="stripe_identity",
            session_id=session_id, detail=event_type)
    return {"status": "ok"}


async def require_kyc_verified(
    current_user: User = Depends(get_current_user),
):
    """Dependency to gate live trading + paid features on KYC completion."""
    status_val = getattr(current_user, "kyc_status", None) or "not_started"
    if status_val != "verified":
        raise HTTPException(
            status_code=403,
            detail="kyc_required",
        )
    return current_user
