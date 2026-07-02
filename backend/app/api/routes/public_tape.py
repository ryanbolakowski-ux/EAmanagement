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


def _fmt_price(price: float) -> str:
    """Comma-grouped, 2dp — '30,528.75'. Matches the frontend's
    toLocaleString(2dp) so fallback and live quotes look identical."""
    return f"{price:,.2f}"


def _fetch_quotes() -> list[dict]:
    """Blocking bulk fetch + parse (runs inside asyncio.to_thread).

    Returns [] rather than raising for per-symbol problems; only a total
    yfinance failure propagates to the caller's except.
    """
    import yfinance as yf  # local import: keep module import cheap at startup

    # 2-day daily pull gives prev close + latest close in one batched call.
    # threads=False: threads=True triggers the "dict changed size during
    # iteration" race in yfinance 1.3.0 (see engines/options/momentum_scanner).
    df = yf.download(
        tickers=" ".join(sym for sym, _ in _TAPE_SYMBOLS),
        period="2d", interval="1d",
        group_by="ticker", auto_adjust=False, progress=False,
        threads=False,
    )
    quotes: list[dict] = []
    for yf_sym, display in _TAPE_SYMBOLS:
        try:
            if yf_sym not in df.columns.levels[0]:
                continue
            sub = df[yf_sym].dropna()
            if len(sub) < 2:
                continue
            prev_close = float(sub.iloc[-2]["Close"])
            last_close = float(sub.iloc[-1]["Close"])
            if (
                not math.isfinite(prev_close) or not math.isfinite(last_close)
                or prev_close <= 0 or last_close <= 0
            ):
                continue
            quotes.append({
                "symbol": display,
                "price": _fmt_price(last_close),
                "change_pct": round((last_close - prev_close) / prev_close * 100.0, 2),
            })
        except Exception as sym_exc:  # one bad symbol never kills the tape
            logger.warning(f"[public-tape] skipping {yf_sym}: {sym_exc}")
            continue
    return quotes


@router.get("/tape")
async def public_tape():
    """Real quote strip for the public landing hero. Never raises, never 500s
    — worst case is {"live": false, "quotes": []} and the frontend keeps its
    static fallback tape."""
    global _last_good, _last_good_at
    try:
        cached = _cache.get(_CACHE_KEY)
        if cached is not None:
            return cached

        # Single-flight: only ONE request per cold-cache window runs the
        # multi-second yfinance download; concurrent callers queue on the
        # lock and are served from the cache the winner just filled.
        async with _fetch_lock:
            cached = _cache.get(_CACHE_KEY)  # re-check: filled while we waited?
            if cached is not None:
                return cached

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
                return payload

            # Failure (or every symbol NaN'd out): serve stale-if-any, else a
            # clean dead-tape response. Always HTTP 200 — decorative endpoint.
            if _last_good is not None:
                if time.monotonic() - _last_good_at <= _STALE_MAX_S:
                    return _last_good
                # Too old to honestly badge as live: same real quotes, but
                # live=False so the frontend drops the LIVE pip.
                return {**_last_good, "live": False}
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
