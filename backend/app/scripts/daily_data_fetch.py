#!/usr/bin/env python3
"""Daily data fetcher - downloads 1m bars from Yahoo Finance into candle_cache.

Used by the FastAPI lifespan to keep candle_cache fresh, and by the one-off
backfill_candle_cache.py script to fill historical gaps.
"""
import asyncio
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from app.database import async_session_factory
from loguru import logger

INSTRUMENTS = {
    "ES": "ES=F",
    "NQ": "NQ=F",
    "RTY": "RTY=F",
    "YM": "YM=F",
}

# Yahoo allows pulling 1m data in 7-day windows. Use 7 daily so each run can
# self-heal recent gaps without going past the API's window.
DAILY_LOOKBACK_DAYS = 7


async def fetch_range_for_instrument(
    db,
    instrument: str,
    yahoo_sym: str,
    start: datetime,
    end: datetime,
) -> int:
    """Fetch [start, end) 1m bars from Yahoo and upsert into candle_cache.

    Returns the number of rows the INSERT touched (DO NOTHING means already-cached
    rows are skipped). Yahoo only serves 1m data in <=7-day windows, so callers
    must chunk wider ranges.
    """
    ticker = yf.Ticker(yahoo_sym)
    df = ticker.history(
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="1m",
    )
    if df is None or df.empty:
        logger.warning(f"No Yahoo data for {instrument} {start.date()}..{end.date()}")
        return 0

    df.columns = [c.lower() for c in df.columns]
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC")

    rows = []
    for ts, row in df.iterrows():
        py_ts = ts.to_pydatetime()
        if py_ts.tzinfo is None:
            py_ts = py_ts.replace(tzinfo=timezone.utc)
        rows.append({
            "sym": instrument,
            "inst": instrument,
            "ts": py_ts,
            "o": float(row["open"]),
            "h": float(row["high"]),
            "l": float(row["low"]),
            "c": float(row["close"]),
            "v": int(row.get("volume", 0) or 0),
        })

    if not rows:
        return 0

    await db.execute(
        text("""
            INSERT INTO candle_cache (symbol, instrument, timestamp, open, high, low, close, volume)
            VALUES (:sym, :inst, :ts, :o, :h, :l, :c, :v)
            ON CONFLICT ON CONSTRAINT uq_symbol_timestamp DO NOTHING
        """),
        rows,
    )
    await db.commit()
    return len(rows)


async def fetch_and_store_daily():
    """Fetch the last DAILY_LOOKBACK_DAYS days of 1m data for all instruments."""
    now = datetime.now(timezone.utc)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    start = end - timedelta(days=DAILY_LOOKBACK_DAYS)

    logger.info(f"Daily data fetch: {start.date()} to {end.date()}")

    async with async_session_factory() as db:
        for instrument, yahoo_sym in INSTRUMENTS.items():
            try:
                n = await fetch_range_for_instrument(db, instrument, yahoo_sym, start, end)
                logger.info(f"Daily fetch: {instrument} - {n} bars upserted")
            except Exception as e:
                await db.rollback()
                logger.error(f"Daily fetch failed for {instrument}: {e}")


async def run_daily_loop():
    """Run the daily fetch on startup and then every 24 hours at 5 AM UTC."""
    # Wait 30 seconds for app to fully start
    await asyncio.sleep(30)

    while True:
        try:
            await fetch_and_store_daily()
        except Exception as e:
            logger.error(f"Daily data fetch error: {e}")

        # Calculate seconds until next 5 AM UTC (= midnight EST)
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(hour=5, minute=0, second=0, microsecond=0)
        wait_seconds = (tomorrow - now).total_seconds()
        logger.info(f"Next daily fetch in {wait_seconds/3600:.1f} hours")
        await asyncio.sleep(wait_seconds)


if __name__ == "__main__":
    asyncio.run(fetch_and_store_daily())
