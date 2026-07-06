"""Wall-Street analyst consensus via FMP /stable (price-target-consensus +
grades-consensus). Powers the plain-English long-term hold target in
'Analyze any ticker' (owner request 2026-07-06: "find the target price from
google or wherever" — this is the wherever, on the plan already paid for).

Cached 12h per symbol (consensus moves slowly); no-coverage results are
negative-cached 1h so small-caps don't re-hit the API on every click. Never
raises — None means 'no analyst coverage' and callers degrade gracefully."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger

from app.engines.data_feeds.fmp_feed import _env_api_key, _get_session

_BASE = "https://financialmodelingprep.com/stable"
_TTL = 12 * 3600.0
_NEG_TTL = 3600.0
_CACHE: dict = {}  # symbol -> (expires_epoch, view | None)


async def _get_rows(path: str, symbol: str, timeout_s: float = 5.0):
    key = _env_api_key()
    if not key:
        return None
    import aiohttp

    session = _get_session()
    async with session.get(
        f"{_BASE}/{path}",
        params={"symbol": symbol, "apikey": key},
        timeout=aiohttp.ClientTimeout(total=timeout_s),
    ) as resp:
        if resp.status != 200:
            logger.warning(f"[fmp-analyst] {path} {symbol}: HTTP {resp.status}")
            return None
        return await resp.json(content_type=None)


async def get_analyst_view(symbol: str) -> Optional[dict]:
    """{'target','target_high','target_low','rating','analysts'} or None."""
    sym = (symbol or "").strip().upper()
    if not sym:
        return None
    now = time.time()
    hit = _CACHE.get(sym)
    if hit and hit[0] > now:
        return hit[1]
    try:
        pt = await _get_rows("price-target-consensus", sym)
        row = pt[0] if isinstance(pt, list) and pt else None
        target = (row or {}).get("targetConsensus") or (row or {}).get("targetMedian")
        if not target or float(target) <= 0:
            _CACHE[sym] = (now + _NEG_TTL, None)  # no coverage (e.g. micro-caps)
            return None
        view = {
            "target": float(target),
            "target_high": (row or {}).get("targetHigh"),
            "target_low": (row or {}).get("targetLow"),
            "rating": None,
            "analysts": None,
        }
        try:
            gr = await _get_rows("grades-consensus", sym)
            g = gr[0] if isinstance(gr, list) and gr else {}
            counts = [int(g.get(k) or 0) for k in
                      ("strongBuy", "buy", "hold", "sell", "strongSell")]
            view["analysts"] = sum(counts) or None
            view["rating"] = g.get("consensus") or None
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # rating is garnish; the target is the meal
        _CACHE[sym] = (now + _TTL, view)
        return view
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[fmp-analyst] {sym} failed ({type(e).__name__}: {e})")
        _CACHE[sym] = (now + 600.0, None)
        return None
