"""Lightweight fundamentals lookup via yfinance.

For the StocksToTrade-style scanner we need:
  • Float size (shares available for trading — <10M = squeeze candidate)
  • 52-week high (breakout target)
  • Average volume (vs current — measures unusual activity)
  • Recent news headlines (catalyst trigger)

yfinance .info pulls everything in one shot. Cached in-process for 1h to
keep the scanner's per-ticker overhead low. SEC EDGAR direct pulls would
be more authoritative but the 1-h cache + yfinance is enough for momentum
scanning where you want a fresh-but-not-realtime fundamentals read.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from loguru import logger

import yfinance as yf


@dataclass
class Fundamentals:
    ticker: str
    market_cap: Optional[float]
    float_shares: Optional[float]
    shares_outstanding: Optional[float]
    avg_volume_10d: Optional[float]
    avg_volume_3m: Optional[float]
    fifty_two_week_high: Optional[float]
    fifty_two_week_low: Optional[float]
    price: Optional[float]


# {ticker: (fetched_at, Fundamentals)}
# TTLCache (was a bare dict): the scanner sweeps a wide ticker universe, so
# entries for tickers that drop out of the universe were never pruned (TTL
# only checked on read). maxsize=2048 bounds it; the manual _TTL freshness
# check below is unchanged (same get/set semantics).
from app.core.ttl_cache import TTLCache
_cache: TTLCache = TTLCache(maxsize=2048, ttl_seconds=3600)
_TTL = timedelta(hours=1)


async def get_fundamentals(ticker: str) -> Optional[Fundamentals]:
    ticker = ticker.upper()
    now = datetime.now(timezone.utc)
    hit = _cache.get(ticker)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    try:
        info = await asyncio.to_thread(lambda: yf.Ticker(ticker).info)
    except Exception as e:
        logger.warning(f"[Fundamentals] yfinance fetch failed for {ticker}: {e}")
        return None
    if not info:
        return None

    f = Fundamentals(
        ticker=ticker,
        market_cap=info.get("marketCap"),
        float_shares=info.get("floatShares"),
        shares_outstanding=info.get("sharesOutstanding"),
        avg_volume_10d=info.get("averageVolume10days") or info.get("averageDailyVolume10Day"),
        avg_volume_3m=info.get("averageVolume") or info.get("averageDailyVolume3Month"),
        fifty_two_week_high=info.get("fiftyTwoWeekHigh"),
        fifty_two_week_low=info.get("fiftyTwoWeekLow"),
        price=info.get("currentPrice") or info.get("regularMarketPrice"),
    )
    _cache[ticker] = (now, f)
    return f


# ── News catalyst detection ──────────────────────────────────────────────

# Words/phrases that signal a positive catalyst — Tim Sykes's playbook.
# These are deliberately broad; false-positive cost is low (skip a trade)
# while false-negative cost is high (miss a runner).
POSITIVE_CATALYST_KEYWORDS = [
    "fda approval", "fda approved", "fda clearance", "fast track",
    "breakthrough designation", "orphan drug",
    "earnings beat", "earnings surprise", "beats estimates",
    "raises guidance", "raised guidance", "upgrade",
    "phase 3 success", "phase iii success", "topline data",
    "contract awarded", "partnership", "acquisition", "to acquire",
    "merger", "strategic agreement", "letter of intent",
    "patent granted", "patent issued",
    "uplisting", "uplist to nasdaq",
    "stock split", "split announcement",
    "buyback", "share repurchase",
    "record revenue", "record sales",
    "regulatory approval",
]

NEGATIVE_CATALYST_KEYWORDS = [
    "fda rejection", "complete response letter", "crl",
    "phase 3 failure", "phase iii failure",
    "delisting", "delist", "going concern",
    "bankruptcy", "chapter 11",
    "fraud", "investigation", "subpoena", "lawsuit",
    "missed earnings", "earnings miss", "below estimates",
    "lowered guidance", "guidance cut", "downgrade",
    "dilution", "stock offering", "secondary offering", "atm offering",
    "halted", "trading halt",
]


@dataclass
class CatalystHit:
    headline: str
    published_at: datetime
    direction: str   # 'positive' | 'negative'
    matched_keyword: str


async def detect_catalyst(ticker: str, lookback_hours: int = 48) -> Optional[CatalystHit]:
    """Pull recent news for `ticker` and return the most-impactful catalyst
    within `lookback_hours`. Returns None if no qualifying headline found."""
    try:
        news = await asyncio.to_thread(lambda: yf.Ticker(ticker).news)
    except Exception as e:
        logger.warning(f"[Catalyst] yfinance news fetch failed for {ticker}: {e}")
        return None
    if not news:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    for item in news:
        try:
            ts = item.get("providerPublishTime") or item.get("pubTime") or 0
            if isinstance(ts, str):
                continue
            pub = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            if pub < cutoff:
                continue
            title = (item.get("title") or "").lower()
            for kw in POSITIVE_CATALYST_KEYWORDS:
                if kw in title:
                    return CatalystHit(headline=item.get("title", ""),
                                        published_at=pub, direction="positive",
                                        matched_keyword=kw)
            for kw in NEGATIVE_CATALYST_KEYWORDS:
                if kw in title:
                    return CatalystHit(headline=item.get("title", ""),
                                        published_at=pub, direction="negative",
                                        matched_keyword=kw)
        except Exception:
            continue
    return None
