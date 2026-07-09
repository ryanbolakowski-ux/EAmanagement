"""Momentum / gap scanner — the StocksToTrade Oracle clone.

What it catches (in priority order):
  1. Pre-market gappers — stocks up ≥ min_gap_pct vs prior close, with
     min_premarket_volume to confirm institutional/retail interest.
  2. Intraday parabolic continuations — stocks up ≥ 10% on the day with
     fresh volume bursts.
  3. News-catalyzed runners — stocks with big % move + volume z-score > 3.

Universe: Polygon's "snapshot/locale/us/markets/stocks/tickers" returns
every US-listed stock's current day stats (day's open, prev close, volume).
We filter to:
  • price between $1 and $20  (the Tim Sykes "easy-momentum" range)
  • avg daily volume > 500K   (need real liquidity)
  • change > min_gap_pct
  • premarket/intraday volume confirmation

For Polygon free-tier users this endpoint returns delayed (~15 min) data.
For paid plans it's live. Either way the morning gappers stay gappy
through 10:00 ET so a 15-min delay still lets us beat StocksToTrade's
10:30 alert by an hour.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Literal
from loguru import logger

import httpx

from app.config import settings
from app.engines.options.polygon_throttle import gate as _poly_gate


@dataclass
class MomentumHit:
    ticker: str
    price: float
    prev_close: float
    change_pct: float
    day_volume: int
    pct_of_avg_volume: float
    score: float
    catalyst: Literal["gap", "intraday_runner", "news_burst", "afterhours"]
    note: str


# Cache the universe snapshot for 60s so back-to-back scans don't burn
# Polygon's rate budget. The snapshot endpoint is heavy.
_snapshot_cache: dict = {"fetched_at": None, "data": None}
_SNAPSHOT_TTL_SEC = 60



# --- polygon-grouped-daily-source ---
import requests as _requests
import os as _os
from datetime import datetime as _dt, timedelta as _td

_POLYGON_KEY_FOR_GROUPED = _os.environ.get("POLYGON_API_KEY", "")
_POLYGON_GROUPED_URL = "https://api.polygon.io/v2/aggs/grouped/locale/us/market/stocks/{date}"
_MIN_DOLLAR_VOL = 1_000_000  # was 10M — lowered so GIPR-class microcaps surface


def _polygon_grouped_two_days():
    """Pull the two most recent trading days from Polygon grouped-daily.
    Returns (today_by_ticker, prev_by_ticker), or ({}, {}) on failure."""
    if not _POLYGON_KEY_FOR_GROUPED:
        return {}, {}
    days = []
    today = _dt.utcnow().date()
    for back in range(1, 10):
        d = (today - _td(days=back)).strftime("%Y-%m-%d")
        try:
            r = _requests.get(
                _POLYGON_GROUPED_URL.format(date=d),
                params={"adjusted": "true", "apiKey": _POLYGON_KEY_FOR_GROUPED},
                timeout=15,
            )
            if r.status_code != 200:
                continue
            j = r.json()
            if j.get("status") in ("OK", "DELAYED") and (j.get("resultsCount") or 0) > 100:
                days.append({row.get("T"): row for row in (j.get("results") or []) if row.get("T")})
            if len(days) >= 2:
                break
        except Exception:
            continue
    return (days[0], days[1]) if len(days) >= 2 else ({}, {})
# --- end polygon-grouped-daily-source ---

def _maybe_universe_compare(rows: list) -> None:
    """[universe-compare] shadow hook (TRACK fmp-universe): when
    SARO_UNIVERSE=polygon and SARO_UNIVERSE_SHADOW=fmp, fire-and-forget an FMP
    universe fetch + one structured comparison log line right after the REAL
    Polygon snapshot is built. Fully try/excepted — can never touch the scan."""
    try:
        from app.engines.data_feeds.fmp_universe import maybe_spawn_universe_compare
        maybe_spawn_universe_compare(rows)
    except Exception:
        pass


async def _fetch_market_snapshot() -> list[dict]:
    """Pull recent quotes + prev-close for the momentum universe via yfinance.

    Polygon's snapshot endpoint requires a paid tier. yfinance is free,
    has no rate limit on bulk downloads, and pulls 300+ tickers in ~30s.

    Returns rows shaped like Polygon's snapshot so the rest of the
    scanner code doesn't care which source it came from:
        {ticker, day: {c, v}, prevDay: {c, v},
         lastTrade: {p}, todaysChangePerc}

    SARO_UNIVERSE=fmp (default polygon) routes to the FMP-sourced universe
    (app.engines.data_feeds.fmp_universe) with graceful fallback to the
    Polygon path below on ANY failure or a too-thin build.
    """
    now = datetime.now(timezone.utc)
    source = (_os.environ.get("SARO_UNIVERSE", "polygon") or "polygon").strip().lower()
    if (_snapshot_cache["data"] is not None
            and _snapshot_cache["fetched_at"]
            and _snapshot_cache.get("source", "polygon") == source
            and (now - _snapshot_cache["fetched_at"]).total_seconds() < _SNAPSHOT_TTL_SEC):
        return _snapshot_cache["data"]

    if source == "fmp":
        try:
            from app.engines.data_feeds.fmp_universe import (
                fetch_fmp_universe, FMP_MIN_UNIVERSE_ROWS,
            )
            fmp_rows = await fetch_fmp_universe()
            if fmp_rows and len(fmp_rows) >= FMP_MIN_UNIVERSE_ROWS:
                _snapshot_cache["data"] = fmp_rows
                _snapshot_cache["fetched_at"] = now
                _snapshot_cache["source"] = "fmp"
                logger.info(f"[Momentum] FMP universe: {len(fmp_rows)} tickers (SARO_UNIVERSE=fmp)")
                return fmp_rows
            logger.warning(f"[Momentum] FMP universe too thin "
                           f"({len(fmp_rows or [])} rows) — falling back to Polygon path")
        except Exception as e:
            logger.warning(f"[Momentum] FMP universe failed "
                           f"({type(e).__name__}: {e}) — falling back to Polygon path")

    # Polygon grouped-daily (Stocks Starter): ~12k US tickers per call
    today_map, prev_map = await asyncio.to_thread(_polygon_grouped_two_days)
    if today_map and prev_map:
        rows = []
        for ticker, today in today_map.items():
            prev = prev_map.get(ticker)
            if not prev:
                continue
            try:
                price = float(today.get("c") or 0)
                prev_close = float(prev.get("c") or 0)
                day_vol = int(today.get("v") or 0)
                prev_vol = int(prev.get("v") or 0)
                if price <= 0 or prev_close <= 0 or day_vol <= 0:
                    continue
                if price * day_vol < _MIN_DOLLAR_VOL:
                    continue
                change_pct = (price - prev_close) / prev_close * 100.0
                rows.append({
                    "ticker": ticker,
                    "day":     {"c": price, "v": day_vol},
                    "prevDay": {"c": prev_close, "v": prev_vol},
                    "lastTrade": {"p": price},
                    "todaysChangePerc": change_pct,
                })
            except Exception:
                continue
        if len(rows) > 200:
            _snapshot_cache["data"] = rows
            _snapshot_cache["fetched_at"] = now
            # Tag with the REQUESTED source, not the path that produced the
            # rows: under SARO_UNIVERSE=fmp these fallback rows are what the
            # fmp source serves right now, and tagging them "polygon" would
            # make the cache check above never hit — every snapshot call
            # (17 per funnel cycle) would re-run the two heavy grouped calls.
            _snapshot_cache["source"] = source
            logger.info(f"[Momentum] Polygon grouped-daily: {len(rows)} liquid tickers (>$10M/day)")
            _maybe_universe_compare(rows)
            return rows
        logger.warning(f"[Momentum] Polygon returned only {len(rows)} rows, falling back to yfinance")


    from app.engines.options.expanded_universe import EXPANDED_UNIVERSE as MOMENTUM_UNIVERSE
    import yfinance as yf
    import pandas as pd

    try:
        # 2-day pull lets us compute prev close + today's price + day volume.
        # group_by="ticker" gives a hierarchical DF we can iterate.
        df = await asyncio.to_thread(
            lambda: yf.download(
                tickers=" ".join(MOMENTUM_UNIVERSE),
                period="2d", interval="1d",
                group_by="ticker", auto_adjust=False, progress=False,
                threads=False,  # threads=True triggers "dict changed size during iteration" race in yfinance 1.3.0
            )
        )
    except Exception as e:
        logger.error(f"[Momentum] yfinance bulk download failed: {e}")
        return []

    rows = []
    for ticker in MOMENTUM_UNIVERSE:
        try:
            if ticker not in df.columns.levels[0]:
                continue
            sub = df[ticker].dropna()
            if len(sub) < 2:
                continue
            prev = sub.iloc[-2]
            today = sub.iloc[-1]
            price = float(today["Close"])
            prev_close = float(prev["Close"])
            day_vol = int(today["Volume"]) if not pd.isna(today["Volume"]) else 0
            prev_vol = int(prev["Volume"]) if not pd.isna(prev["Volume"]) else 0
            if prev_close <= 0:
                continue
            change_pct = (price - prev_close) / prev_close * 100.0
            rows.append({
                "ticker": ticker,
                "day":     {"c": price, "v": day_vol},
                "prevDay": {"c": prev_close, "v": prev_vol},
                "lastTrade": {"p": price},
                "todaysChangePerc": change_pct,
            })
        except Exception:
            continue

    _snapshot_cache["data"] = rows
    _snapshot_cache["fetched_at"] = now
    _snapshot_cache["source"] = source  # requested source — see grouped-path note
    logger.info(f"[Momentum] yfinance pulled {len(rows)} tickers (from {len(MOMENTUM_UNIVERSE)} requested)")
    if rows:
        _maybe_universe_compare(rows)
    return rows


async def _fetch_prev_close_fallback(symbol: str) -> Optional[dict]:
    """Per-ticker prev-close fallback when the snapshot endpoint is gated.

    Returns the same shape we'd extract from the snapshot:
        {ticker, day_close, prev_close, change_pct, day_volume}
    """
    await _poly_gate.acquire()
    url = f"https://api.polygon.io/v2/aggs/ticker/{symbol.upper()}/prev"
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, params={"adjusted": "true", "apiKey": settings.POLYGON_API_KEY})
            if r.status_code != 200:
                return None
            results = (r.json() or {}).get("results") or []
            if not results:
                return None
            row = results[0]
            return {"prev_close": row.get("c"), "prev_vol": row.get("v")}
    except Exception:
        return None


def _score(change_pct: float, vol_ratio: float, catalyst: str) -> float:
    """Rank hits. Bigger move + higher relative volume = higher score.
    News-bursts get a bonus because they have a fresh narrative."""
    base = abs(change_pct)
    base += min(vol_ratio, 10) * 2.0   # cap so a 100x volume day doesn't dominate
    if catalyst == "news_burst":
        base += 5
    if catalyst == "afterhours":
        base -= 2  # less actionable
    return base


async def scan_for_momentum(
    *,
    min_change_pct: float = 10.0,
    max_change_pct: float = 30.0,   # skip blow-off tops > this
    min_price: float = 1.0,
    max_price: float = 20.0,
    min_day_volume: int = 500_000,
    min_vol_ratio: float = 2.5,     # day_volume / prev_volume (raised 2026-06-16 to cut weak gappers)
    top_k: int = 10,
    include_negative: bool = True,  # catch -X% drops too (for puts)
) -> list[MomentumHit]:
    """Scan all US stocks. Return the top-K momentum hits matching the
    Tim Sykes / STT criteria: $1-$20, 10%+ move, 500K+ volume, volume
    surge vs prior day."""
    snapshot = await _fetch_market_snapshot()
    if not snapshot:
        # No snapshot tier — return empty rather than burning the per-ticker
        # rate budget. Caller can fall back to a smaller universe.
        logger.warning("[Momentum] no snapshot data — skipping scan")
        return []

    hits: list[MomentumHit] = []
    now_et_hour = datetime.now(timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    ).hour

    for row in snapshot:
        try:
            ticker = row.get("ticker") or ""
            day    = row.get("day") or {}
            prev   = row.get("prevDay") or {}
            last_trade = row.get("lastTrade") or {}
            price       = float(last_trade.get("p") or day.get("c") or 0)
            prev_close  = float(prev.get("c") or 0)
            day_volume  = int(day.get("v") or 0)
            prev_volume = int(prev.get("v") or 0)
            change_pct  = float(row.get("todaysChangePerc") or 0)

            # Filter
            if not (min_price <= price <= max_price):
                continue
            if day_volume < min_day_volume:
                continue
            magnitude = abs(change_pct)
            if magnitude < min_change_pct:
                continue
            if magnitude > max_change_pct:
                # Blow-off top territory — already a parabolic move, high
                # reversal risk. Skip.
                continue
            if change_pct < 0 and not include_negative:
                continue
            if prev_volume <= 0:
                # No completed-session volume baseline (recent IPO, or an FMP
                # mover missing from the Polygon prev map — fmp_universe emits
                # prevDay.v=0 by its never-fabricate contract). The old
                # `or 1` fallback fabricated a monster vol_ratio here and let
                # such rows auto-pass the surge gate; a surge that can't be
                # verified is not a surge — skip.
                continue
            vol_ratio = day_volume / prev_volume
            if vol_ratio < min_vol_ratio:
                continue

            # Classify catalyst by time-of-day
            if now_et_hour < 9 or (now_et_hour == 9 and datetime.now().minute < 30):
                catalyst = "gap"
            elif now_et_hour >= 16:
                catalyst = "afterhours"
            elif vol_ratio >= 5.0:
                catalyst = "news_burst"
            else:
                catalyst = "intraday_runner"

            note = (f"{ticker} {('UP' if change_pct > 0 else 'DOWN')} "
                     f"{magnitude:.1f}% on {vol_ratio:.1f}x volume "
                     f"@ ${price:.2f} (prev ${prev_close:.2f})")

            hits.append(MomentumHit(
                ticker=ticker, price=price, prev_close=prev_close,
                change_pct=change_pct, day_volume=day_volume,
                pct_of_avg_volume=vol_ratio,
                score=_score(change_pct, vol_ratio, catalyst),
                catalyst=catalyst, note=note,
            ))
        except Exception:
            continue

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]
