"""End-of-day digest scheduler.

Fires once per weekday at 4:30 PM US/Eastern with a per-user P&L summary
(emailed via Resend). Imported by main.py's lifespan.
"""
import asyncio
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy import select, text

from app.database import async_session_factory
from app.models.user import User
from app.services.email import send_daily_digest_email


ET = ZoneInfo("America/New_York")
DIGEST_TIME = dtime(hour=16, minute=30)  # 4:30 PM ET


def _seconds_until_next_fire(now_utc: datetime) -> float:
    """How long to sleep until the next 4:30 PM ET. If it's already past 4:30
    today, schedule for tomorrow at the same hour. Returns whole seconds."""
    now_et = now_utc.astimezone(ET)
    target = now_et.replace(
        hour=DIGEST_TIME.hour, minute=DIGEST_TIME.minute, second=0, microsecond=0
    )
    if now_et >= target:
        target = target + timedelta(days=1)
    return (target - now_et).total_seconds()


async def _send_digest_for_today():
    """Compute today's P&L per user (across paper + live), then email each
    user who actually traded today. Users with zero trades get nothing —
    no point spamming inboxes with empty summaries."""
    today_et = datetime.now(ET).date()
    date_str = today_et.strftime("%a, %b %-d, %Y")

    async with async_session_factory() as db:
        # Pull every trade closed today, grouped by user. exit_time is stored
        # as tz-aware UTC; we convert in SQL so day boundaries match ET.
        result = await db.execute(text("""
            SELECT
                user_id,
                COUNT(*)                                                     AS total,
                COUNT(*) FILTER (WHERE net_pnl > 0)                          AS wins,
                COUNT(*) FILTER (WHERE net_pnl < 0)                          AS losses,
                COALESCE(SUM(net_pnl), 0)                                    AS net,
                COALESCE(MAX(net_pnl), 0)                                    AS best,
                COALESCE(MIN(net_pnl), 0)                                    AS worst,
                COALESCE(SUM(net_pnl) FILTER (WHERE mode = 'paper'), 0)      AS paper_pnl,
                COALESCE(SUM(net_pnl) FILTER (WHERE mode = 'live'),  0)      AS live_pnl
            FROM trades
            WHERE exit_time IS NOT NULL
              AND (exit_time AT TIME ZONE 'America/New_York')::date = :today
            GROUP BY user_id
        """), {"today": today_et})
        per_user = result.fetchall()

        if not per_user:
            logger.info("[DailyDigest] No trades closed today — nothing to send.")
            return

        # Pull the user records for everyone with trades today
        user_ids = [str(r[0]) for r in per_user]
        urows = await db.execute(
            select(User).where(User.id.in_(user_ids))
        )
        users_by_id = {str(u.id): u for u in urows.scalars().all()}

    sent = 0
    for row in per_user:
        uid, total, wins, losses, net, best, worst, paper_pnl, live_pnl = row
        u = users_by_id.get(str(uid))
        if not u or not u.email:
            continue
        closed = (wins or 0) + (losses or 0)
        win_rate = ((wins or 0) / closed * 100) if closed else 0.0
        try:
            ok = send_daily_digest_email(
                to=u.email,
                username=u.username or "trader",
                date_str=date_str,
                total_trades=int(total or 0),
                wins=int(wins or 0),
                losses=int(losses or 0),
                net_pnl=float(net or 0),
                win_rate=win_rate,
                largest_win=float(best or 0),
                largest_loss=float(worst or 0),
                paper_pnl=float(paper_pnl or 0),
                live_pnl=float(live_pnl or 0),
            )
            if ok:
                sent += 1
        except Exception as e:
            logger.error(f"[DailyDigest] Failed for {u.email}: {e}")

    logger.info(f"[DailyDigest] Sent {sent} digest email(s) for {date_str}")


async def run_daily_digest_loop():
    """Long-running task — sleeps until 4:30 PM ET each day, fires the digest."""
    logger.info("[DailyDigest] Scheduler started — daily fire at 4:30 PM ET.")
    while True:
        try:
            wait_s = _seconds_until_next_fire(datetime.now(ZoneInfo("UTC")))
            logger.info(f"[DailyDigest] Sleeping {wait_s:.0f}s until next fire.")
            await asyncio.sleep(wait_s)
            if datetime.now(ET).weekday() >= 5:
                logger.info("[digest] weekend — skipping daily summary (weekdays only)")
            else:
                await _send_digest_for_today()
        except asyncio.CancelledError:
            logger.info("[DailyDigest] Scheduler cancelled.")
            raise
        except Exception as e:
            logger.error(f"[DailyDigest] Unexpected error: {e}")
            # Don't tight-loop on persistent errors — back off 5 minutes.
            await asyncio.sleep(300)
