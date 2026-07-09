"""Public landing-page ticker tape — real quotes for the LandingV2 hero crawl.

GET /api/v1/public/tape — NO auth. The landing page is public/unauthenticated,
so this endpoint must be callable without a token. It is strictly decorative
and strictly read-only:

  * The symbol list is a FIXED server-side map (yfinance ticker -> display
    symbol). It is never built from user input — the route takes no params —
    so there is nothing to inject and nothing to enumerate.
  * One batched yf.download() per cache window (60s TTL), run inside
    asyncio.to_thread so the sync yfinance call NEVER blocks the event loop.
    A module-level asyncio.Lock single-flights the cache-miss path, so N
    concurrent cold-cache requests share ONE download instead of launching N.
  * This route can never 500: any failure returns the last-good payload if we
    have one (badged live:false once >15 min stale), else a clean
    {"live": false, "quotes": []} — the frontend keeps its static fallback
    tape and simply doesn't show the LIVE pip.
"""
import asyncio
import math
import time
from datetime import datetime, timezone

from fastapi import APIRouter
from loguru import logger

from app.core.ttl_cache import TTLCache

router = APIRouter()

# Fixed display order for the tape: the Theta futures book first, then the
# megacap tape everyone scans for, then oil + gold. yfinance continuous
# futures tickers use the "=F" suffix; we display the bare root (ES, NQ, ...).
_TAPE_SYMBOLS: list[tuple[str, str]] = [
    ("ES=F", "ES"), ("NQ=F", "NQ"), ("YM=F", "YM"), ("RTY=F", "RTY"),
    ("SPY", "SPY"), ("QQQ", "QQQ"), ("NVDA", "NVDA"), ("AAPL", "AAPL"),
    ("MSFT", "MSFT"), ("TSLA", "TSLA"), ("AMZN", "AMZN"), ("META", "META"),
    ("GOOGL", "GOOGL"), ("AMD", "AMD"), ("CL=F", "CL"), ("GC=F", "GC"),
]

# One payload cached for 60s. maxsize=4 is just headroom — there is exactly
# one key, but TTLCache requires a bound and 4 costs nothing.
_CACHE_KEY = "tape"
_cache = TTLCache(maxsize=4, ttl_seconds=60.0)

# Single-flight guard for the cache-miss path. This is a PUBLIC unauthenticated
# route: without the lock, EVERY concurrent request during a cold/expired-cache
# window would launch its own multi-second sequential yf.download inside the
# shared default to_thread executor (min(32, cpu+4) threads) — a traffic spike
# or a trivial curl loop could saturate that executor for the whole app and
# hammer Yahoo from the prod IP. With it, exactly one fetch runs per window;
# everyone else parks on the lock and is served from the freshly-filled cache.
_fetch_lock = asyncio.Lock()

# Last successful payload, kept as a stale-but-real fallback for the failure
# path (Yahoo hiccup, rate limit, ...). Decorative surface, so a few-minutes-
# old real quote beats an empty band — but once it's older than _STALE_MAX_S
# we stop *calling* it live: the quotes are still served, with live=False, so
# the frontend keeps the tape but drops the LIVE pip instead of lying about
# hours-old prices.
_last_good: dict | None = None
_last_good_at: float = 0.0  # time.monotonic() of the last successful fetch
_STALE_MAX_S = 15 * 60.0

# REALTIME-FEED-V1: previous close per DISPLAY symbol, refreshed by every
# successful _fetch_quotes(). The realtime overlay needs it to recompute
# change_pct against a live last price (the cached quote only carries the
# formatted price string). With the flag off this map is written, never read.
_prev_close: dict[str, float] = {}

# Display symbols the realtime store can serve DIRECTLY (stocks/ETFs on the
# Polygon stocks ws cluster — yfinance ticker == display symbol). Futures
# rows (ES/NQ/YM/RTY) and commodities (CL/GC) are deliberately NOT overlaid:
# the store could only offer a scaled ETF proxy for them, and
# SIGNAL-PRICE-ALIGN-V1 taught us never to display a proxy-scaled price as a
# real futures price without a real anchor to validate it against. Their
# yfinance quote (delayed but REAL) stays.
_RT_STOCK_SYMBOLS = tuple(disp for yf_sym, disp in _TAPE_SYMBOLS if yf_sym == disp)


def _fmt_price(price: float) -> str:
    """Comma-grouped, 2dp — '30,528.75'. Matches the frontend's
    toLocaleString(2dp) so fallback and live quotes look identical."""
    return f"{price:,.2f}"


def _fetch_quotes() -> list[dict]:
    """Blocking bulk fetch + parse (runs inside asyncio.to_thread).

    FMP-first (2026-07-09): Yahoo throttles the prod IP — the old batched
    yf.download measured 703s, so the tape sat on its own single-flight lock
    for minutes. FMP daily bars are 10-min cached (fetch_daily_bars_sync) and
    quote-short is one fast call per symbol. Symbols without FMP coverage on
    this plan (NQ/YM/RTY/CL/GC futures) are skipped rather than holding the
    tape hostage. Returns [] rather than raising for per-symbol problems.
    """
    from datetime import date as _date, datetime as _dt, timedelta as _td, timezone as _tz

    from app.engines.data_feeds.fmp_feed import (
        fetch_daily_bars_sync, fetch_quote_short_price_sync)

    # Futures with a working FMP symbol on this plan (NQUSD is premium-gated).
    _fmp_futures = {"ES=F": "ESUSD"}

    end_iso = _date.today().isoformat()
    start_iso = (_date.today() - _td(days=10)).isoformat()
    quotes: list[dict] = []
    for src_sym, display in _TAPE_SYMBOLS:
        try:
            fmp_sym = _fmp_futures.get(src_sym) or (src_sym if src_sym == display else None)
            if not fmp_sym:
                continue  # no FMP coverage for this symbol on this plan
            bars = fetch_daily_bars_sync(fmp_sym, start_iso, end_iso)  # 10-min cached
            if len(bars) < 2:
                continue
            prev_close = float(bars[-2]["c"])
            last_close = float(bars[-1]["c"])
            live = fetch_quote_short_price_sync(fmp_sym)
            if live and float(live) > 0:
                # +12h nudges the ET-midnight-encoded bar timestamp into the
                # correct calendar day regardless of DST.
                last_bar_day = _dt.fromtimestamp(
                    bars[-1]["t"] / 1000 + 43200, tz=_tz.utc).date().isoformat()
                if last_bar_day == end_iso:
                    last_close = float(live)   # today's bar exists: live refines it
                else:
                    prev_close = last_close    # pre-open: yesterday becomes the anchor
                    last_close = float(live)
            if (
                not math.isfinite(prev_close) or not math.isfinite(last_close)
                or prev_close <= 0 or last_close <= 0
            ):
                continue
            _prev_close[display] = prev_close  # anchor for the realtime overlay
            quotes.append({
                "symbol": display,
                "price": _fmt_price(last_close),
                "change_pct": round((last_close - prev_close) / prev_close * 100.0, 2),
            })
        except Exception as sym_exc:  # one bad symbol never kills the tape
            logger.warning(f"[public-tape] skipping {src_sym}: {sym_exc}")
            continue
    return quotes


def _overlay_realtime_sync(payload: dict) -> dict:
    """REALTIME-FEED-V1 (blocking half, runs in asyncio.to_thread): return a
    NEW payload with stock/ETF quotes replaced by seconds-fresh prices from
    the in-process realtime store. The cached payload is NEVER mutated (it is
    shared via _cache/_last_good). Quotes the store can't serve fresh
    (futures rows, unsubscribed symbols, stale store) pass through untouched.
    """
    from app.engines.data_feeds.realtime_feed import get_fresh_price, request_symbols

    # Ask the feed to carry the tape's stock symbols. Idempotent set-add —
    # the first request after boot subscribes them, every later call no-ops.
    request_symbols(_RT_STOCK_SYMBOLS)

    quotes: list[dict] = []
    overlaid = 0
    for q in payload.get("quotes", []):
        try:
            sym = q.get("symbol")
            prev = _prev_close.get(sym)
            px = get_fresh_price(sym) if sym in _RT_STOCK_SYMBOLS else None
            if px and px > 0 and prev and prev > 0:
                quotes.append({
                    "symbol": sym,
                    "price": _fmt_price(px),
                    "change_pct": round((px - prev) / prev * 100.0, 2),
                })
                overlaid += 1
            else:
                quotes.append(q)
        except Exception:  # one bad overlay never kills the tape
            quotes.append(q)
    if not overlaid:
        return payload
    # At least one quote is genuinely live now — badge accordingly.
    return {
        **payload,
        "quotes": quotes,
        "live": True,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }


async def _with_realtime_overlay(payload: dict) -> dict:
    """Flag gate for the realtime overlay. REALTIME_FEED off (the default)
    returns the payload UNCHANGED — same object, zero extra work — so the
    route is byte-identical to pre-feed behavior. Never raises."""
    try:
        from app.engines.data_feeds.realtime_feed import realtime_enabled
        if not realtime_enabled():
            return payload
        # to_thread: get_fresh_price is lock-cheap, but request_symbols +
        # formatting run per-quote and this is a public route — keep the
        # event loop untouched like the yfinance fetch path does.
        return await asyncio.to_thread(_overlay_realtime_sync, payload)
    except Exception as exc:
        logger.warning(f"[public-tape] realtime overlay failed: {exc}")
        return payload


@router.get("/tape")
async def public_tape():
    """Real quote strip for the public landing hero. Never raises, never 500s
    — worst case is {"live": false, "quotes": []} and the frontend keeps its
    static fallback tape."""
    global _last_good, _last_good_at
    try:
        cached = _cache.get(_CACHE_KEY)
        if cached is not None:
            return await _with_realtime_overlay(cached)

        # Single-flight: only ONE request per cold-cache window runs the
        # multi-second yfinance download; concurrent callers queue on the
        # lock and are served from the cache the winner just filled.
        async with _fetch_lock:
            cached = _cache.get(_CACHE_KEY)  # re-check: filled while we waited?
            if cached is not None:
                return await _with_realtime_overlay(cached)

            try:
                quotes = await asyncio.to_thread(_fetch_quotes)
            except Exception as exc:
                logger.warning(f"[public-tape] yfinance bulk fetch failed: {exc}")
                quotes = []

            if quotes:
                payload = {
                    "as_of": datetime.now(timezone.utc).isoformat(),
                    "live": True,
                    "quotes": quotes,
                }
                _cache[_CACHE_KEY] = payload
                _last_good = payload
                _last_good_at = time.monotonic()
                return await _with_realtime_overlay(payload)

            # Failure (or every symbol NaN'd out): serve stale-if-any, else a
            # clean dead-tape response. Always HTTP 200 — decorative endpoint.
            # (The realtime overlay still applies: with the ws feed live, a
            # Yahoo outage no longer forces stale stock prices on the tape.)
            if _last_good is not None:
                if time.monotonic() - _last_good_at <= _STALE_MAX_S:
                    return await _with_realtime_overlay(_last_good)
                # Too old to honestly badge as live: same real quotes, but
                # live=False so the frontend drops the LIVE pip.
                return await _with_realtime_overlay({**_last_good, "live": False})
            return {
                "as_of": datetime.now(timezone.utc).isoformat(),
                "live": False,
                "quotes": [],
            }
    except Exception as exc:  # belt & braces: this route must never 500
        logger.warning(f"[public-tape] unexpected error: {exc}")
        return {
            "as_of": datetime.now(timezone.utc).isoformat(),
            "live": False,
            "quotes": [],
        }
