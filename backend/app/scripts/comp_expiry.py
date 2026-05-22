"""Comp expiry daemon — runs once per hour. Any user whose
comp_expires_at is in the past gets dropped back to free_trial.

Sends an email letting them know their free access ended (best-effort)."""
import asyncio
from datetime import datetime, timezone
from loguru import logger
from sqlalchemy import text

from app.database import async_session_factory


async def expire_due_comps_once() -> int:
    """Run once. Returns the count of users dropped."""
    async with async_session_factory() as db:
        # Find users whose comp expired
        rows = (await db.execute(text("""
            SELECT id, email, username, subscription_tier
              FROM users
             WHERE comp_expires_at IS NOT NULL
               AND comp_expires_at < NOW()
               AND stripe_subscription_id IS NULL
               AND subscription_tier != 'free_trial'
        """))).all()

        if not rows:
            return 0

        dropped = 0
        for r in rows:
            old_tier = r.subscription_tier
            await db.execute(text("""
                UPDATE users
                   SET subscription_tier = 'free_trial',
                       comp_granted_at = NULL,
                       comp_expires_at = NULL,
                       comp_granted_by = NULL,
                       comp_note = NULL
                 WHERE id = :uid
            """), {"uid": str(r.id)})
            dropped += 1
            logger.info(f"[CompExpiry] dropped {r.email} from {old_tier} → free_trial")

            # Best-effort email
            try:
                from app.services.email import _send, _logo_header
                from app.config import settings
                _send(
                    r.email,
                    "Your Theta Algos free access has ended",
                    f"""
                    <div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
                      {_logo_header()}
                      <h1 style="margin:0 0 4px;font-size:22px;">Free access ended</h1>
                      <p style="margin:0 0 14px;color:#475569;">Your complimentary {old_tier.replace('_', ' ').title()} access just expired. Your account is now on Tier 1 (Free Trial).</p>
                      <p style="margin:0 0 14px;color:#475569;">Want to keep the scanner running? <a href="{settings.FRONTEND_URL}/app/profile" style="color:#7c3aed;font-weight:700;">Pick a plan in your account settings</a>.</p>
                      <p style="margin:18px 0 0;color:#94a3b8;font-size:11px;">— Theta Algos</p>
                    </div>
                    """,
                )
            except Exception as e:
                logger.warning(f"[CompExpiry] couldn\'t notify {r.email}: {e}")

        await db.commit()
        return dropped


async def run_comp_expiry_loop():
    """Background coroutine — checks every hour for expired comps."""
    logger.info("[CompExpiry] daemon started, checking hourly")
    while True:
        try:
            n = await expire_due_comps_once()
            if n:
                logger.info(f"[CompExpiry] dropped {n} expired comps")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[CompExpiry] tick error: {e}")
        await asyncio.sleep(3600)  # 1 hour
