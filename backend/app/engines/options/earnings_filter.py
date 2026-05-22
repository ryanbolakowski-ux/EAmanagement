"""Earnings filter for options strategies.

Trading equity options through an earnings announcement is a fundamentally
different bet from a directional swing trade: IV crush, gap risk, and binary
outcomes make standard delta-based sizing useless. The `options_avoid_earnings_days`
config field on each strategy says "skip new entries within N days of earnings"
— this module enforces that.

Source: yfinance's `Ticker.calendar` returns the next earnings date for an
equity. It's free, no API key needed, and reliable enough for stocks with
liquid options. We cache results in `earnings_calendar` (DB) and refresh
once every 24h per ticker.

Note: yfinance returns timezone-naive dates. We treat them as US/Eastern
since that's where most US equity earnings are timed.
"""
import asyncio
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from loguru import logger

import yfinance as yf
from sqlalchemy import text

from app.database import async_session_factory


# Cache TTL — yfinance is rate-limited and we don't want to thrash it
_EARNINGS_CACHE_TTL_HOURS = 24


async def get_next_earnings_date(ticker: str) -> Optional[date]:
    """Return the next earnings date for `ticker`, or None if unknown.

    Caches results in the `earnings_calendar` DB table for 24h. Falls
    back to live yfinance fetch on cache miss. Returns None (skip the
    filter) if yfinance can't find earnings data — better to take a
    possibly-bad trade than block every options trade because we can't
    look up earnings for some illiquid name."""
    ticker = ticker.upper()

    async with async_session_factory() as db:
        # Recent cache hit?
        row = (await db.execute(text("""
            SELECT earnings_date FROM earnings_calendar
             WHERE ticker = :t
               AND fetched_at > NOW() - INTERVAL '24 hours'
             ORDER BY earnings_date ASC
             LIMIT 1
        """), {"t": ticker})).fetchone()
        if row and row.earnings_date:
            return row.earnings_date

    # Cache miss — fetch from yfinance (offload to thread, it's sync)
    try:
        cal = await asyncio.to_thread(lambda: yf.Ticker(ticker).calendar)
    except Exception as e:
        logger.warning(f"[Earnings] yfinance fetch failed for {ticker}: {e}")
        return None

    earnings_date: Optional[date] = None
    if isinstance(cal, dict):
        # yfinance returns {'Earnings Date': [datetime, ...], ...}
        ed = cal.get("Earnings Date")
        if isinstance(ed, list) and ed:
            try:
                earnings_date = ed[0].date() if hasattr(ed[0], "date") else date.fromisoformat(str(ed[0])[:10])
            except Exception:
                earnings_date = None

    if earnings_date:
        try:
            async with async_session_factory() as db:
                await db.execute(text("""
                    INSERT INTO earnings_calendar (ticker, earnings_date, fetched_at)
                    VALUES (:t, :d, NOW())
                    ON CONFLICT (ticker, earnings_date)
                        DO UPDATE SET fetched_at = NOW()
                """), {"t": ticker, "d": earnings_date})
                await db.commit()
        except Exception as e:
            logger.warning(f"[Earnings] cache write failed for {ticker}: {e}")

    return earnings_date


async def is_near_earnings(ticker: str, on_date: date,
                            avoid_days: int = 7) -> tuple[bool, Optional[date]]:
    """Returns (is_near, earnings_date).

    `is_near=True` when the next earnings event is within `avoid_days` of
    `on_date` (looking forward only — past earnings don't matter). Caller
    should skip the trade.

    Falls open (returns False) when we can't determine earnings — better to
    let a trade through than block every options trade because an API hiccupped.
    """
    if avoid_days <= 0:
        return False, None
    earnings_date = await get_next_earnings_date(ticker)
    if earnings_date is None:
        return False, None
    delta = (earnings_date - on_date).days
    return (0 <= delta <= avoid_days), earnings_date
