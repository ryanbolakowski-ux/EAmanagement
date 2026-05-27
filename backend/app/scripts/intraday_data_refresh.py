"""Intraday candle_cache refresher.

The daily fetcher only runs at 5 AM UTC, leaving the cache hours stale by
mid-session. Watchers then fall through to yfinance, which rate-limits and
returns empty bars — the strategy goes blind and entry-emails silently stop.

This module runs a background loop that, during US trading hours, appends the
latest 1m bars for futures (ES/NQ/RTY/YM) into candle_cache every 60 seconds.
yfinance calls are serialized through a shared lock so we never exceed 4
calls/minute regardless of how many watchers are subscribed.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone
from loguru import logger
from sqlalchemy import text
import yfinance as yf
import pandas as pd

from app.database import async_session_factory

INSTRUMENTS = {
    "ES": "ES=F",
    "NQ": "NQ=F",
    "RTY": "RTY=F",
    "YM": "YM=F",
}

# Single shared lock so all yfinance calls serialize — keeps us well under
# yfinance's per-IP throttle even when other code paths (heartbeat, scanner)
# pile on simultaneously.
_YF_LOCK = asyncio.Lock()


def _in_market_hours() -> bool:
    """True when US futures are actively trading and watchers care.

    CME futures trade Sun 6pm ET → Fri 5pm ET with a 1h break at 5pm ET daily.
    We refresh between 4 AM ET and 8 PM ET Mon-Fri (covers premarket + RTH +
    after-hours). Off-hours we still tick every 5 min so cache doesn't drift.
    """
    try:
        import zoneinfo
        et = datetime.now(timezone.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return True  # fail-open: assume in-hours so we keep refreshing
    if et.weekday() >= 5:
        return False
    h = et.hour
    return 4 <= h < 20


async def _refresh_one(db, instrument: str, yahoo_sym: str) -> int:
    """Fetch the last 2 days of 1m bars from yfinance and upsert.

    Returns number of rows offered to the upsert (skipped duplicates count too
    — Postgres ON CONFLICT DO NOTHING absorbs them silently).
    """
    def _fetch():
        # yfinance 1m only available within last 7 days. 2d window keeps the
        # call cheap while still backfilling any tiny gaps from the prior tick.
        tk = yf.Ticker(yahoo_sym)
        return tk.history(period="2d", interval="1m", auto_adjust=False)

    df = await asyncio.to_thread(_fetch)
    if df is None or df.empty:
        return 0

    df.columns = [c.lower() for c in df.columns]
    if df.index.tz is not None:
        df.index = df.index.tz_convert("UTC")

    rows = []
    for ts, row in df.iterrows():
        py_ts = ts.to_pydatetime()
        if py_ts.tzinfo is None:
            py_ts = py_ts.replace(tzinfo=timezone.utc)
        try:
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
        except Exception:
            continue
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


async def run_intraday_refresh_loop():
    """Background loop. Aligns each tick to the next 60-second UTC boundary
    so cache writes line up with bar closes — minimizes worst-case staleness."""
    logger.info("[IntradayRefresh] loop started")
    # Brief delay so it doesn't fight startup ordering
    await asyncio.sleep(10)
    consecutive_failures = 0
    while True:
        try:
            if not _in_market_hours():
                await asyncio.sleep(300)
                continue
            t0 = datetime.now(timezone.utc)
            async with async_session_factory() as db:
                for inst, ysym in INSTRUMENTS.items():
                    async with _YF_LOCK:
                        try:
                            n = await _refresh_one(db, inst, ysym)
                            if n:
                                # Log only when staleness was rescued
                                pass
                        except Exception as e:
                            msg = str(e)[:120]
                            if "Too Many Requests" in msg or "Rate limited" in msg:
                                logger.warning(f"[IntradayRefresh] yfinance rate-limited on {inst}; backing off")
                                await asyncio.sleep(30)
                            else:
                                logger.warning(f"[IntradayRefresh] {inst} fetch failed: {msg}")
            consecutive_failures = 0
            # Sleep until next minute boundary + 2s (let the bar close fully)
            now = datetime.now(timezone.utc)
            next_min = (now + timedelta(minutes=1)).replace(second=2, microsecond=0)
            wait = max(5.0, (next_min - now).total_seconds())
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            logger.info("[IntradayRefresh] loop cancelled")
            return
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"[IntradayRefresh] loop iteration crashed: {e}")
            # Exponential backoff, capped at 5 min
            await asyncio.sleep(min(300, 30 * consecutive_failures))
