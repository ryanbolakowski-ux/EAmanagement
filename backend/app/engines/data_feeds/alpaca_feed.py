"""Alpaca IEX real-time market-data adapter (free tier).

The Polygon "Stocks" plan and the TwelveData plan we use are BOTH ~15-min
delayed (Polygon real-time aggregates return 403 "not entitled"), which made
the futures Account-Signals emails fire ~15 min late. Alpaca's FREE tier serves
real-time **IEX** trades/bars, which — for ultra-liquid SPY/QQQ/IWM/DIA — are
accurate to the penny and seconds-fresh. This module fetches those 1-min ETF
bars so the futures runner can scale them to the futures price level via the
existing dynamic ``get_proxy_scale()`` (ES->SPY, NQ->QQQ, RTY->IWM, YM->DIA).

Credentials come from the environment ONLY (never hardcoded):
    ALPACA_API_KEY     -> APCA-API-KEY-ID
    ALPACA_API_SECRET  -> APCA-API-SECRET-KEY

If either is missing the adapter returns ``None`` silently so the system keeps
using the existing Polygon/yfinance fallback until the user adds keys. It NEVER
raises — any error (network, auth, parse) yields ``None``.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from loguru import logger

# Free real-time feed. "iex" is the entitlement the Alpaca free tier grants;
# "sip" (full consolidated tape) requires a paid Alpaca data subscription.
_ALPACA_FEED = "iex"
_BARS_URL = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
_TIMEOUT_SEC = 8.0


def fetch_alpaca_bars(symbol: str, timeframe: str = "1Min", limit: int = 200):
    """Real-time 1-min bars from Alpaca's IEX feed (free tier).

    GET https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1Min&feed=iex&limit=N
    Auth headers: APCA-API-KEY-ID, APCA-API-SECRET-KEY
    (from env ALPACA_API_KEY / ALPACA_API_SECRET).

    Returns a DataFrame indexed by a tz-aware UTC ``DatetimeIndex`` with columns
    ``open/high/low/close/volume``, or ``None``. ``feed=iex`` is the FREE
    real-time feed. Never raises — returns ``None`` on any error/missing-key.
    """
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        # No keys configured yet -> stay silent; caller falls back to Polygon/yf.
        return None

    sym = (symbol or "").upper().strip()
    if not sym:
        return None

    try:
        import pandas as pd
    except Exception as e:  # pragma: no cover - pandas is always present in prod
        logger.warning(f"[alpaca] pandas import failed ({type(e).__name__}: {e})")
        return None

    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }
    params = {
        "timeframe": timeframe,
        "feed": _ALPACA_FEED,
        "limit": int(limit),
        "sort": "asc",
    }
    url = _BARS_URL.format(symbol=sym)

    # Prefer httpx (already a project dep); fall back to requests. Either way an
    # 8s timeout, and never propagate an exception to the caller.
    try:
        try:
            import httpx
            resp = httpx.get(url, headers=headers, params=params, timeout=_TIMEOUT_SEC)
            status = resp.status_code
            payload = resp.json() if status == 200 else None
        except ImportError:  # pragma: no cover - httpx is a project dep
            import requests
            resp = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT_SEC)
            status = resp.status_code
            payload = resp.json() if status == 200 else None
    except Exception as e:
        logger.warning(f"[alpaca] {sym} {timeframe} fetch error {type(e).__name__}: {e}")
        return None

    if status != 200:
        logger.warning(f"[alpaca] {sym} {timeframe} HTTP {status} feed={_ALPACA_FEED}")
        return None

    bars = (payload or {}).get("bars") or []
    if not bars:
        logger.info(f"[alpaca] {sym} {timeframe} no bars returned feed={_ALPACA_FEED}")
        return None

    try:
        df = pd.DataFrame(bars)
        # Alpaca bar fields: t (RFC-3339 ts), o, h, l, c, v (+ n, vw we ignore).
        df["timestamp"] = pd.to_datetime(df["t"], utc=True)
        df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                                "c": "close", "v": "volume"})
        df = df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]
        for col in ("open", "high", "low", "close", "volume"):
            df[col] = df[col].astype(float)
        df = df.sort_index()
    except Exception as e:
        logger.warning(f"[alpaca] {sym} {timeframe} parse error {type(e).__name__}: {e}")
        return None

    if df.empty:
        return None

    latest_ts = df.index[-1]
    age_sec = (datetime.now(timezone.utc) - latest_ts.to_pydatetime()).total_seconds()
    logger.info(
        f"[alpaca] {sym} {timeframe} latest_bar={latest_ts.isoformat()} "
        f"age={age_sec:.0f}s feed={_ALPACA_FEED}"
    )
    return df
