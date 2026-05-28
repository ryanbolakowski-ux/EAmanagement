"""Dynamic futures <-> ETF-proxy price scaling.

Polygon's Stocks plan can't serve CME futures, so an ETF proxy (QQQ for NQ,
SPY for ES, etc.) approximates the price action. But the proxy trades at a very
different price LEVEL than the future, and the ratio DRIFTS over time:
NQ/QQQ was ~31 when the old hardcoded constants were written and is ~41 now.
Using a stale constant produced ~25% wrong prices; using no scaling at all (the
polygon_feed path) produced ~41x wrong prices.

This computes the CURRENT ratio from a real futures reference (yfinance "=F")
divided by the ETF, cached for 1h so we make at most one quote per instrument
per hour. Falls back to a recent constant if the live quote is unavailable.
"""
import threading
import time as _time
from loguru import logger

# instrument -> (real_futures_yahoo_symbol, etf_proxy_symbol)
_PROXY_PAIR = {
    "ES": ("ES=F", "SPY"),
    "NQ": ("NQ=F", "QQQ"),
    "RTY": ("RTY=F", "IWM"),
    "YM": ("YM=F", "DIA"),
}

# Fallbacks (refreshed 2026-05) used only if the live ratio can't be fetched.
_FALLBACK_SCALE = {"ES": 10.0, "NQ": 41.0, "RTY": 9.0, "YM": 95.0}

_cache: dict = {}          # inst -> (ts, scale)
_lock = threading.Lock()
_TTL = 3600.0              # 1 hour


def get_proxy_scale(instrument: str) -> float:
    """Live (futures / ETF) price ratio for converting proxy bars to futures
    price levels. Returns 1.0 for non-proxied instruments."""
    inst = (instrument or "").upper()
    if inst not in _PROXY_PAIR:
        return 1.0
    now = _time.time()
    hit = _cache.get(inst)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]
    with _lock:
        hit = _cache.get(inst)
        if hit and (_time.time() - hit[0]) < _TTL:
            return hit[1]
        fut_sym, etf_sym = _PROXY_PAIR[inst]
        scale = _FALLBACK_SCALE.get(inst, 1.0)
        try:
            import yfinance as yf
            fut = float(yf.Ticker(fut_sym).fast_info.last_price)
            etf = float(yf.Ticker(etf_sym).fast_info.last_price)
            if fut and etf and etf > 0:
                scale = round(fut / etf, 3)
                logger.info(
                    f"[proxy_scale] {inst}: {fut_sym}={fut:.2f} / {etf_sym}={etf:.2f} "
                    f"-> scale={scale} (fallback was {_FALLBACK_SCALE.get(inst)})"
                )
            else:
                logger.warning(f"[proxy_scale] {inst}: bad quotes fut={fut} etf={etf}; using fallback {scale}")
        except Exception as e:
            logger.warning(f"[proxy_scale] {inst}: live ratio failed ({type(e).__name__}: {e}); using fallback {scale}")
        _cache[inst] = (_time.time(), scale)
        return scale
