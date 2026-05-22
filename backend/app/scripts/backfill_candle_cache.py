#!/usr/bin/env python3
"""One-off backfill of candle_cache from Yahoo Finance.

Pulls 1m bars in 7-day chunks (Yahoo's max window) for each instrument and
upserts them. Reuses fetch_range_for_instrument from daily_data_fetch so the
INSERT/conflict semantics stay identical.

Usage (inside the backend container):
    python -m app.scripts.backfill_candle_cache 2026-04-14 2026-05-02
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from loguru import logger

from app.database import async_session_factory
from app.scripts.daily_data_fetch import INSTRUMENTS, fetch_range_for_instrument


CHUNK_DAYS = 7


async def backfill(start: datetime, end: datetime) -> None:
    logger.info(f"Backfilling candle_cache: {start.date()} to {end.date()}")
    async with async_session_factory() as db:
        for instrument, yahoo_sym in INSTRUMENTS.items():
            cur = start
            total = 0
            while cur < end:
                chunk_end = min(cur + timedelta(days=CHUNK_DAYS), end)
                try:
                    n = await fetch_range_for_instrument(
                        db, instrument, yahoo_sym, cur, chunk_end,
                    )
                    total += n
                    logger.info(
                        f"  {instrument}: {cur.date()}..{chunk_end.date()} -> {n} bars"
                    )
                except Exception as e:
                    await db.rollback()
                    logger.error(
                        f"  {instrument}: {cur.date()}..{chunk_end.date()} failed: {e}"
                    )
                cur = chunk_end
            logger.info(f"{instrument} backfill total: {total} bars")


def _parse(d: str) -> datetime:
    return datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m app.scripts.backfill_candle_cache <start YYYY-MM-DD> <end YYYY-MM-DD>")
        sys.exit(1)
    asyncio.run(backfill(_parse(sys.argv[1]), _parse(sys.argv[2])))
