"""Background broker-balance sync loop (SYSTEMS-CHECK-V3.2).

Keeps ``broker_accounts.cached_equity / cached_buying_power / cached_balance_at``
fresh during US market hours by polling each active account's broker every
~15 min. Before this loop existed, balances refreshed ONLY on-demand (dashboard
balance view / scanner view / admin "resync" Fix), so the admin Systems Check
could not meaningfully judge broker-sync health — a stale cache just meant
"nobody has looked recently", not a stall. With this loop running, a stale cache
during market hours IS a real stall worth flagging, so broker_sync's freshness
check is meaningful again.

Cadence: refresh every ``REFRESH_INTERVAL_SEC`` while the market is open (using
the SAME open/closed definition the Systems Check uses, so freshness and the
health check stay aligned and broker_sync never false-yellows); idle-poll every
``IDLE_POLL_SEC`` while closed (balances don't move). Per-account failures are
isolated (one bad account never stalls the rest) and the loop never raises —
broken creds / broker outages simply leave the cache stale, which the Systems
Check surfaces as yellow after the freshness window.
"""
import asyncio
from datetime import datetime, timezone
from loguru import logger
from sqlalchemy import text

from app.database import async_session_factory

REFRESH_INTERVAL_SEC = 900   # 15 min while the market is open
IDLE_POLL_SEC = 300          # re-check every 5 min while the market is closed
BOOT_DELAY_SEC = 75          # let the restart connection-pool burst settle first


def _market_open_now() -> bool:
    """The SAME open/closed definition the admin Systems Check uses (intraday
    market window + NYSE holiday), so the loop refreshes exactly when broker_sync
    expects fresh data — no false yellow at the seams. Fail-open (refresh)."""
    try:
        from zoneinfo import ZoneInfo
        from app.engines.options.premarket_scheduler import _within_market_window
        from app.core.sc_logic import is_market_holiday
        et = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
        return bool(_within_market_window(et)) and not is_market_holiday(et.date())
    except Exception:
        return True  # fail-open: refresh rather than let balances silently drift


async def _refresh_all_accounts() -> tuple[int, int]:
    """Refresh every active broker account owner, each in an isolated DB session
    so one failed fetch can't poison the others. Returns (ok_count, total)."""
    from app.api.routes.scanner import _refresh_broker_balance
    async with async_session_factory() as db:
        rows = (await db.execute(text(
            "SELECT DISTINCT user_id FROM broker_accounts WHERE is_active=true"
        ))).fetchall()
    user_ids = [r.user_id for r in rows]
    ok = 0
    for uid in user_ids:
        try:
            async with async_session_factory() as db:   # isolated per account
                eq, _bp, _pl = await _refresh_broker_balance(db, uid)
            if eq is not None:
                ok += 1
            else:
                logger.warning(f"[broker-sync] no balance data for user={uid} (creds/outage?)")
        except Exception as e:
            logger.warning(f"[broker-sync] refresh failed for user={uid}: {str(e)[:120]}")
    return ok, len(user_ids)


async def run_broker_balance_sync_loop():
    """Background task: refresh broker balances on a ~15-min cadence during
    market hours. Started from main.py's lifespan; cancelled on shutdown."""
    logger.info("[broker-sync] loop started")
    await asyncio.sleep(BOOT_DELAY_SEC)
    consecutive_failures = 0
    while True:
        try:
            if not _market_open_now():
                await asyncio.sleep(IDLE_POLL_SEC)
                continue
            ok, total = await _refresh_all_accounts()
            if total:
                logger.info(f"[broker-sync] refreshed {ok}/{total} active broker account(s)")
            consecutive_failures = 0
            await asyncio.sleep(REFRESH_INTERVAL_SEC)
        except asyncio.CancelledError:
            logger.info("[broker-sync] loop cancelled")
            return
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"[broker-sync] loop iteration crashed: {e}")
            await asyncio.sleep(min(300, 30 * consecutive_failures))
