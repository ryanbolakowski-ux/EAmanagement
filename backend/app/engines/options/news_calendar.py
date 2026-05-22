"""News blackout calendar — skip scanner runs 30 min before/after big events.

What counts as 'big' for short-term scanners:
  • FOMC (rate decisions + minutes) — 14:00 ET
  • CPI / Core CPI — 08:30 ET
  • PPI / Core PPI — 08:30 ET
  • NFP (Non-Farm Payrolls) — 08:30 ET, first Friday
  • Jobless Claims — 08:30 ET Thursdays
  • GDP — 08:30 ET
  • Retail Sales — 08:30 ET
  • Powell speeches (when scheduled)

Source: ForexFactory's free JSON calendar at nfs.faireconomy.media — refreshes
every 4-6 hours. We cache results in the news_blackouts DB table.

Fallback when the feed is unreachable: hardcoded recurring events (NFP first
Friday, FOMC dates from a static calendar) so the scanner never accidentally
trades through an event because we couldn't reach the network.
"""
import asyncio
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from loguru import logger

import httpx
from sqlalchemy import text

from app.database import async_session_factory


FFX_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Hardcoded 2026 economic calendar — every red-folder US event with
# its release date and time. Sourced from BLS / Fed schedules published
# Dec 2025. Update annually around Christmas for the next year.
#
# All times are 14:00 ET for FOMC, 08:30 ET for everything else (the
# BLS standard release time). PCE comes out at 08:30 ET as well.

# (date, hour_et, minute_et, event_name)
RED_FOLDER_2026 = [
    # FOMC rate decisions (8 per year)
    (date(2026, 1, 28),  14, 0, "FOMC Statement"),
    (date(2026, 3, 18),  14, 0, "FOMC Statement"),
    (date(2026, 4, 29),  14, 0, "FOMC Statement"),
    (date(2026, 6, 17),  14, 0, "FOMC Statement"),
    (date(2026, 7, 29),  14, 0, "FOMC Statement"),
    (date(2026, 9, 16),  14, 0, "FOMC Statement"),
    (date(2026, 10, 28), 14, 0, "FOMC Statement"),
    (date(2026, 12, 9),  14, 0, "FOMC Statement"),
    # CPI (BLS) — monthly, ~mid-month
    (date(2026, 1, 13), 8, 30, "CPI"),
    (date(2026, 2, 11), 8, 30, "CPI"),
    (date(2026, 3, 12), 8, 30, "CPI"),
    (date(2026, 4, 14), 8, 30, "CPI"),
    (date(2026, 5, 13), 8, 30, "CPI"),
    (date(2026, 6, 11), 8, 30, "CPI"),
    (date(2026, 7, 15), 8, 30, "CPI"),
    (date(2026, 8, 12), 8, 30, "CPI"),
    (date(2026, 9, 11), 8, 30, "CPI"),
    (date(2026, 10, 15), 8, 30, "CPI"),
    (date(2026, 11, 13), 8, 30, "CPI"),
    (date(2026, 12, 10), 8, 30, "CPI"),
    # PPI (BLS) — usually day after CPI
    (date(2026, 1, 14), 8, 30, "PPI"),
    (date(2026, 2, 12), 8, 30, "PPI"),
    (date(2026, 3, 13), 8, 30, "PPI"),
    (date(2026, 4, 15), 8, 30, "PPI"),
    (date(2026, 5, 14), 8, 30, "PPI"),
    (date(2026, 6, 12), 8, 30, "PPI"),
    (date(2026, 7, 16), 8, 30, "PPI"),
    (date(2026, 8, 13), 8, 30, "PPI"),
    (date(2026, 9, 14), 8, 30, "PPI"),
    (date(2026, 10, 16), 8, 30, "PPI"),
    (date(2026, 11, 16), 8, 30, "PPI"),
    (date(2026, 12, 11), 8, 30, "PPI"),
    # NFP (BLS) — 1st Friday of each month
    (date(2026, 1, 9),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 2, 6),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 3, 6),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 4, 3),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 5, 1),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 6, 5),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 7, 3),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 8, 7),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 9, 4),  8, 30, "Non-Farm Payrolls"),
    (date(2026, 10, 2), 8, 30, "Non-Farm Payrolls"),
    (date(2026, 11, 6), 8, 30, "Non-Farm Payrolls"),
    (date(2026, 12, 4), 8, 30, "Non-Farm Payrolls"),
    # PCE (BEA) — last Friday or last business day of month
    (date(2026, 1, 30),  8, 30, "Core PCE"),
    (date(2026, 2, 27),  8, 30, "Core PCE"),
    (date(2026, 3, 27),  8, 30, "Core PCE"),
    (date(2026, 4, 30),  8, 30, "Core PCE"),
    (date(2026, 5, 29),  8, 30, "Core PCE"),
    (date(2026, 6, 26),  8, 30, "Core PCE"),
    (date(2026, 7, 31),  8, 30, "Core PCE"),
    (date(2026, 8, 28),  8, 30, "Core PCE"),
    (date(2026, 9, 25),  8, 30, "Core PCE"),
    (date(2026, 10, 30), 8, 30, "Core PCE"),
    (date(2026, 11, 25), 8, 30, "Core PCE"),
    (date(2026, 12, 23), 8, 30, "Core PCE"),
    # Retail Sales (Census) — ~15th of next month
    (date(2026, 1, 16), 8, 30, "Retail Sales"),
    (date(2026, 2, 17), 8, 30, "Retail Sales"),
    (date(2026, 3, 17), 8, 30, "Retail Sales"),
    (date(2026, 4, 15), 8, 30, "Retail Sales"),
    (date(2026, 5, 14), 8, 30, "Retail Sales"),
    (date(2026, 6, 16), 8, 30, "Retail Sales"),
    (date(2026, 7, 16), 8, 30, "Retail Sales"),
    (date(2026, 8, 14), 8, 30, "Retail Sales"),
    (date(2026, 9, 15), 8, 30, "Retail Sales"),
    (date(2026, 10, 15), 8, 30, "Retail Sales"),
    (date(2026, 11, 17), 8, 30, "Retail Sales"),
    (date(2026, 12, 15), 8, 30, "Retail Sales"),
    # Advance GDP (BEA) — last Thursday of next month after quarter end
    (date(2026, 1, 29),  8, 30, "GDP Advance"),
    (date(2026, 4, 30),  8, 30, "GDP Advance"),
    (date(2026, 7, 30),  8, 30, "GDP Advance"),
    (date(2026, 10, 29), 8, 30, "GDP Advance"),
]

def _is_high_impact(title: str, impact: str) -> bool:
    """Red folder only — ForexFactory's `impact='high'` flag.

    CPI, PPI, NFP, GDP, FOMC rate decisions, Core PCE, and Retail Sales
    are always tagged 'high' by FFX. Fed speaker calendars and JOLTS
    are tagged 'medium' (orange folder) — those don't move markets
    enough to be worth pausing the scanner, so we ignore them.
    """
    return (impact or "").lower() == "high"


async def refresh_blackouts() -> int:
    """Pull the ForexFactory weekly calendar, dump high-impact USD events
    into news_blackouts. Returns the count of new/refreshed rows."""
    try:
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": "Mozilla/5.0 Theta-Algos news-fetch"}) as c:
            r = await c.get(FFX_JSON)
            r.raise_for_status()
            events = r.json() or []
    except Exception as e:
        logger.warning(f"[NewsCalendar] FFX fetch failed: {e}")
        events = []  # fall through to hardcoded seeding

    saved = 0
    async with async_session_factory() as db:
        for ev in events:
            if (ev.get("country") or "").upper() != "USD":
                continue
            title = ev.get("title") or ""
            impact = ev.get("impact") or ""
            if not _is_high_impact(title, impact):
                continue
            iso = ev.get("date")
            if not iso:
                continue
            try:
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            except Exception:
                continue
            try:
                await db.execute(text("""
                    INSERT INTO news_blackouts
                        (event_name, event_time, severity, source, notes)
                    VALUES (:n, :t, 'high', 'forexfactory', :notes)
                    ON CONFLICT (event_name, event_time) DO NOTHING
                """), {"n": title[:100], "t": dt,
                        "notes": (ev.get("forecast") or "") + "|" + (ev.get("previous") or "")})
                saved += 1
            except Exception as e:
                logger.warning(f"[NewsCalendar] save failed for {title}: {e}")
        # Seed the full hardcoded 2026 red-folder schedule. Each ET wall-
        # clock time is converted to UTC accounting for DST: ET is UTC-5
        # in standard, UTC-4 in daylight savings. We use US/Eastern to
        # produce the right offset automatically.
        from zoneinfo import ZoneInfo as _ZI
        _et = _ZI("America/New_York")
        for d, h, m, name in RED_FOLDER_2026:
            dt_et = datetime(d.year, d.month, d.day, h, m, tzinfo=_et)
            dt_utc = dt_et.astimezone(timezone.utc)
            await db.execute(text("""
                INSERT INTO news_blackouts (event_name, event_time, severity, source)
                VALUES (:name, :t, 'high', 'hardcoded_2026')
                ON CONFLICT (event_name, event_time) DO NOTHING
            """), {"name": name, "t": dt_utc})
        await db.commit()
    return saved


async def is_blackout_active(buffer_min: int = 30, at: Optional[datetime] = None) -> Optional[dict]:
    """Return the active blackout event if `at` (default: now) falls within
    ±buffer_min of any high-impact event. Returns None if clear to trade."""
    at = at or datetime.now(timezone.utc)
    window_start = at - timedelta(minutes=buffer_min)
    window_end   = at + timedelta(minutes=buffer_min)
    async with async_session_factory() as db:
        r = (await db.execute(text("""
            SELECT event_name, event_time, severity
              FROM news_blackouts
             WHERE severity = 'high'
               AND event_time >= :s AND event_time <= :e
             ORDER BY event_time ASC
             LIMIT 1
        """), {"s": window_start, "e": window_end})).fetchone()
    if not r:
        return None
    return {
        "event_name": r.event_name,
        "event_time": r.event_time.isoformat(),
        "severity":   r.severity,
    }


async def next_clear_time(buffer_min: int = 30, after: Optional[datetime] = None) -> datetime:
    """Return the next datetime when the scanner can safely resume after
    a blackout. If no blackout is active, returns `after` (or now)."""
    after = after or datetime.now(timezone.utc)
    block = await is_blackout_active(buffer_min=buffer_min, at=after)
    if not block:
        return after
    event_time = datetime.fromisoformat(block["event_time"].replace("Z", "+00:00"))
    return event_time + timedelta(minutes=buffer_min)
