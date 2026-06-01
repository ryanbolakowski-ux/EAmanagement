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
        # asyncpg can't run multiple commands in one prepared statement, so
        # execute each ';'-separated DDL statement (table + indexes) on its own.
        for _stmt in _KYC_EVENTS_DDL.split(";"):
            if _stmt.strip():
                await db.execute(text(_stmt))
        await db.commit()
        _kyc_table_checked = True
    except Exception as e:
        await db.rollback()  # don't leave the session in an aborted-transaction state
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
    """Return the user's KYC verification status.

    Opportunistic safety net: if the user is currently 'pending' AND we have a
    Stripe session id, pull the authoritative status from Stripe before
    responding. This self-heals the (real, observed) case where Stripe sent us
    a verified webhook but our handler crashed before persisting.
    """
    cur_status = getattr(current_user, "kyc_status", None) or "not_started"
    session_id = getattr(current_user, "kyc_session_id", None)
    verified_at = getattr(current_user, "kyc_verified_at", None)
    verification_url: str | None = None

    # Aggressive sync: previously we only synced on 'pending'. We now ALSO
    # sync on:
    #   * 'requires_input' — the user may have submitted the missing input
    #                        on Stripe's hosted page; we need to re-check.
    #   * verified-with-null-verified_at — data consistency. If the row says
    #                        verified but verified_at is NULL the row was
    #                        partially updated by a half-failed webhook and
    #                        re-pulling from Stripe will repair COALESCE.
    needs_sync = bool(session_id) and (
        cur_status == "pending"
        or cur_status == "requires_input"
        or (cur_status == "verified" and verified_at is None)
    )
    if needs_sync:
        try:
            synced = await sync_kyc_status_from_stripe(
                db, user_id=str(current_user.id), session_id=session_id
            )
            if synced is not None:
                await db.refresh(current_user)
                cur_status = getattr(current_user, "kyc_status", None) or cur_status
        except Exception as _e:
            logger.warning(f"[kyc-status] opportunistic sync failed: {_e}")
    if cur_status == "requires_input" and session_id:
        try:
            import stripe as _stripe
            if STRIPE_IDENTITY_KEY:
                _stripe.api_key = STRIPE_IDENTITY_KEY
                _vs = _stripe.identity.VerificationSession.retrieve(session_id)
                verification_url = getattr(_vs, "url", None)
        except Exception as _e:
            logger.debug(f"[kyc-status] could not fetch verification_url: {_e}")
    return {
        "status": cur_status,
        "verified_at": current_user.kyc_verified_at.isoformat() if getattr(current_user, "kyc_verified_at", None) else None,
        "provider": getattr(current_user, "kyc_provider", None),
        "country": getattr(current_user, "country_code", None),
        "verification_url": verification_url,
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

    # Parse DOB to a real date for asyncpg — string params fail with
    # 'str' object has no attribute 'toordinal' on PG date columns.
    from datetime import date as _kyc_date
    try:
        _dob_date = _kyc_date.fromisoformat(data.date_of_birth)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")
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
            "dob": _dob_date, "cc": data.country_code.upper(),
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
            "dob": _dob_date, "cc": data.country_code.upper(),
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
                "dob": _dob_date, "cc": data.country_code.upper(),
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
    """Stripe Identity webhook receiver.

    Stripe Python SDK v8+ returns event["data"]["object"] as a StripeObject,
    not a plain dict. Calling .get() on it raises AttributeError via the
    StripeObject.__getattr__ shim. This previously caused every webhook to
    500, which left real users stuck at 'pending' forever even after Stripe
    verified them. We now convert StripeObject -> dict via to_dict_recursive()
    before any field access.
    """
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")
    webhook_secret = os.environ.get("STRIPE_IDENTITY_WEBHOOK_SECRET", "")
    logger.info(
        f"[kyc-webhook] received bytes={len(body)} sig_present={bool(sig)} "
        f"secret_configured={bool(webhook_secret)}"
    )
    if not STRIPE_IDENTITY_KEY:
        return {"status": "stub"}
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured.")

    try:
        import stripe
        try:
            event = stripe.Webhook.construct_event(body, sig, webhook_secret)
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"[kyc-webhook] signature verification failed: {e}")
            raise HTTPException(status_code=400, detail="Invalid signature.")
        except Exception as e:
            logger.error(f"[kyc-webhook] construct_event failed: {e}")
            raise HTTPException(status_code=400, detail="Could not parse event.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[kyc-webhook] stripe import / setup failed: {e}")
        raise HTTPException(status_code=500, detail="Internal webhook error.")

    # Pull event_type from either StripeObject attr or dict key
    event_type = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)

    # Extract the inner object. event.data.object is a StripeObject on SDK v8+
    raw_obj = None
    try:
        if hasattr(event, "data") and getattr(event, "data", None) is not None:
            raw_obj = event.data.object if hasattr(event.data, "object") else None
        if raw_obj is None and isinstance(event, dict):
            raw_obj = (event.get("data") or {}).get("object")
    except Exception as e:
        logger.warning(f"[kyc-webhook] could not access event.data.object: {e}")
        raw_obj = None

    # Convert to a plain dict — THIS is the line that fixes the production bug.
    if raw_obj is None:
        obj: dict = {}
    elif hasattr(raw_obj, "to_dict_recursive"):
        try:
            obj = raw_obj.to_dict_recursive()
        except Exception as e:
            logger.warning(f"[kyc-webhook] to_dict_recursive failed: {e}")
            obj = {}
    elif isinstance(raw_obj, dict):
        obj = raw_obj
    else:
        try:
            obj = dict(raw_obj)
        except Exception:
            obj = {}

    session_id = obj.get("id")
    metadata = obj.get("metadata") or {}
    user_id = metadata.get("user_id")
    logger.info(
        f"[kyc-webhook] event_type={event_type} session={session_id} user={user_id}"
    )

    if event_type == "identity.verification_session.verified":
        # Cross-check: the document's country MUST be US. Stripe returns
        # verified_outputs.address.country once the ID has been validated.
        doc_country = None
        try:
            verified = obj.get("verified_outputs") or {}
            doc_country = ((verified.get("address") or {}).get("country") or "").upper() or None
            if not doc_country:
                doc_country = ((verified.get("id_number") or {}).get("country") or "").upper() or None
        except Exception:
            doc_country = None

        if doc_country and doc_country != "US":
            await db.execute(text(
                "UPDATE users SET kyc_status='failed' "
                "WHERE id = :uid AND kyc_session_id = :sid AND kyc_status NOT IN ('verified')"
            ), {"uid": user_id, "sid": session_id})
            await db.commit()
            try:
                await _log_kyc_event(db, user_id=user_id or "", user_email=None,
                    event_type="webhook", status="failed", provider="stripe_identity",
                    session_id=session_id, country=doc_country,
                    detail=f"Stripe verified user but document country is {doc_country}, not US — rejected")
            except Exception: pass
            logger.warning(f"[kyc-webhook] REJECTED user={user_id} session={session_id} doc_country={doc_country} (non-US)")
        else:
            await db.execute(text(
                "UPDATE users SET kyc_status='verified', "
                "  kyc_verified_at=COALESCE(kyc_verified_at, NOW()), "
                "  country_code='US' "
                "WHERE id = :uid AND kyc_session_id = :sid"
            ), {"uid": user_id, "sid": session_id})
            await db.commit()
            try:
                await _log_kyc_event(db, user_id=user_id or "", user_email=None,
                    event_type="webhook", status="verified", provider="stripe_identity",
                    session_id=session_id, country=doc_country or "US",
                    detail=event_type)
            except Exception: pass
            logger.info(f"[kyc-webhook] verified user={user_id} session={session_id} doc_country={doc_country or 'unknown'}")

    elif event_type == "identity.verification_session.requires_input":
        await db.execute(text(
            "UPDATE users SET kyc_status='requires_input' "
            "WHERE id = :uid AND kyc_session_id = :sid AND kyc_status NOT IN ('verified')"
        ), {"uid": user_id, "sid": session_id})
        await db.commit()
        try:
            await _log_kyc_event(db, user_id=user_id or "", user_email=None,
                event_type="webhook", status="requires_input", provider="stripe_identity",
                session_id=session_id, detail=event_type)
        except Exception: pass

    elif event_type == "identity.verification_session.canceled":
        await db.execute(text(
            "UPDATE users SET kyc_status='failed' "
            "WHERE id = :uid AND kyc_session_id = :sid AND kyc_status NOT IN ('verified')"
        ), {"uid": user_id, "sid": session_id})
        await db.commit()
        try:
            await _log_kyc_event(db, user_id=user_id or "", user_email=None,
                event_type="webhook", status="failed", provider="stripe_identity",
                session_id=session_id, detail=event_type)
        except Exception: pass

    elif event_type == "identity.verification_session.processing":
        # Stripe is still working on it — keep us at pending. Audit only.
        await db.execute(text(
            "UPDATE users SET kyc_status='pending' "
            "WHERE id = :uid AND kyc_session_id = :sid AND kyc_status NOT IN ('verified')"
        ), {"uid": user_id, "sid": session_id})
        await db.commit()
        try:
            await _log_kyc_event(db, user_id=user_id or "", user_email=None,
                event_type="webhook", status="pending", provider="stripe_identity",
                session_id=session_id, detail=event_type)
        except Exception: pass

    elif event_type == "identity.verification_session.created":
        # Idempotent — we already created this session on our side. Audit only.
        try:
            await _log_kyc_event(db, user_id=user_id or "", user_email=None,
                event_type="webhook", status="created", provider="stripe_identity",
                session_id=session_id, detail=event_type)
        except Exception: pass

    elif event_type == "identity.verification_session.redacted":
        # Stripe redacted PII per retention policy — we keep our internal status.
        # If they were verified, they stay verified. Audit only.
        try:
            await _log_kyc_event(db, user_id=user_id or "", user_email=None,
                event_type="webhook", status="redacted", provider="stripe_identity",
                session_id=session_id, detail=event_type)
        except Exception: pass

    else:
        logger.info(f"[kyc-webhook] unhandled event_type={event_type} session={session_id}")

    # Always ACK so Stripe doesn't retry forever on event types we don't action.
    return {"status": "ok", "event_type": event_type, "session_id": session_id}


# --- kyc patch v2 (StripeObject-safe webhook + sync helper + admin force-sync) ---
# --- Stripe-side status sync helper + admin force-sync endpoint ---
# Webhook-loss safety net: pull authoritative status from Stripe and reconcile
# the local row. Used opportunistically by GET /status for pending users, and
# explicitly by the admin force-sync endpoint for bulk recovery.

_STRIPE_TO_INTERNAL = {
    "verified": "verified",
    "requires_input": "requires_input",
    "canceled": "failed",
    "processing": "pending",
    "redacted": "verified",  # preserve verified history when PII is purged
}


async def sync_kyc_status_from_stripe(db, user_id: str, session_id: str) -> str | None:
    """Authoritative pull from Stripe. Returns the (possibly updated) internal
    status, or None if Stripe is unreachable / not configured. Safe to call on
    every /status request for pending users — Stripe Identity reads are cheap."""
    if not STRIPE_IDENTITY_KEY or not session_id:
        return None
    try:
        import stripe
        stripe.api_key = STRIPE_IDENTITY_KEY
        vs = stripe.identity.VerificationSession.retrieve(session_id)
    except Exception as e:
        logger.warning(f"[kyc-sync] stripe retrieve failed user={user_id} session={session_id}: {e}")
        return None

    stripe_status = getattr(vs, "status", None) or (vs.get("status") if hasattr(vs, "get") else None)
    internal = _STRIPE_TO_INTERNAL.get(stripe_status)
    if internal is None:
        logger.warning(f"[kyc-sync] unknown stripe_status={stripe_status} user={user_id} session={session_id}")
        return None

    # Country cross-check for verified (consistent with webhook logic)
    doc_country = None
    try:
        vo = getattr(vs, "verified_outputs", None)
        if vo is None and hasattr(vs, "get"):
            vo = vs.get("verified_outputs")
        if vo is not None:
            if hasattr(vo, "to_dict_recursive"):
                vo = vo.to_dict_recursive()
            elif not isinstance(vo, dict):
                try: vo = dict(vo)
                except Exception: vo = {}
            doc_country = ((vo.get("address") or {}).get("country") or "").upper() or None
            if not doc_country:
                doc_country = ((vo.get("id_number") or {}).get("country") or "").upper() or None
    except Exception:
        doc_country = None

    if internal == "verified" and doc_country and doc_country != "US":
        internal = "failed"

    # Idempotent UPDATE: never downgrade verified; only set kyc_verified_at if NULL.
    if internal == "verified":
        await db.execute(text(
            "UPDATE users SET kyc_status='verified', "
            "  kyc_verified_at=COALESCE(kyc_verified_at, NOW()), "
            "  country_code='US' "
            "WHERE id = :uid"
        ), {"uid": user_id})
    else:
        await db.execute(text(
            "UPDATE users SET kyc_status=:st "
            "WHERE id = :uid AND kyc_status NOT IN ('verified')"
        ), {"uid": user_id, "st": internal})
    await db.commit()
    try:
        await _log_kyc_event(db, user_id=user_id, user_email=None,
            event_type="sync", status=internal, provider="stripe_identity",
            session_id=session_id, country=doc_country,
            detail=f"stripe={stripe_status} -> internal={internal}")
    except Exception: pass
    logger.info(f"[kyc-sync] user={user_id} session={session_id} stripe={stripe_status} -> internal={internal}")
    return internal


@router.post("/admin/force-sync")
async def admin_force_sync_kyc(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: bulk-reconcile every pending user that has a Stripe session.
    Used to recover from past webhook delivery failures. Returns one row per
    user with before/after status so the admin can verify the sweep."""
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=403, detail="Admin only.")
    logger.warning(f"[kyc-force-sync] admin={current_user.email} triggered bulk reconciliation")
    rows = (await db.execute(text(
        "SELECT id::text AS id, email, kyc_status, kyc_session_id "
        "FROM users "
        "WHERE kyc_status = 'pending' AND kyc_session_id IS NOT NULL"
    ))).mappings().all()

    results = []
    for r in rows:
        before = r["kyc_status"]
        after = None
        try:
            after = await sync_kyc_status_from_stripe(
                db, user_id=str(r["id"]), session_id=r["kyc_session_id"]
            )
        except Exception as e:
            logger.error(f"[kyc-force-sync] user={r['email']} failed: {e}")
        results.append({
            "email": r["email"],
            "session_id": r["kyc_session_id"],
            "before": before,
            "after": after or before,
        })
    logger.warning(
        f"[kyc-force-sync] swept {len(results)} pending users; "
        f"transitions={sum(1 for x in results if x['before'] != x['after'])}"
    )
    return {"swept": len(results), "results": results}



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
