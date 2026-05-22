import json
"""Universe scanner — runs an options strategy across many tickers and
returns the top-N signals ranked by strength.

Designed for the pre-market scan at ~08:30 ET. Iterates the configured
universe, pulls each ticker's recent bars from Polygon, runs the strategy's
signal logic, and ranks signals by:

    score = abs(actual_delta - target_delta_mid) inverse +
            volume_zscore_at_signal_bar +
            FVG_size_vs_avg_range (when applicable)

The bot then emits the top-K (default 3) as pending trades that wait for
user confirmation before executing.

Polygon-throttled — uses the shared rate gate so multi-ticker scans don't
blow through the free-tier limit (a 50-ticker scan at 4 RPM ≈ 12.5 minutes
on free tier; ~25 seconds on Stocks Starter).
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from typing import Optional
from loguru import logger

import httpx
import pandas as pd

from app.config import settings
from app.engines.options.polygon_throttle import gate as _poly_gate
from app.engines.options.universe import get_universe
from app.engines.backtest_engine.ict_strategy import ICTStrategy
from app.engines.strategy_engine.base_strategy import StrategyConfig, SignalType
from app.engines.data_feeds.polygon_feed import POLYGON_API_KEY


@dataclass
class ScannerHit:
    ticker: str
    direction: str               # 'long' | 'short'
    score: float
    spot: float
    bias: Optional[str]
    reason: str                  # plain-english explanation
    metadata: dict


async def _fetch_bars(ticker: str, lookback_days: int = 5,
                       interval: str = "5m") -> pd.DataFrame:
    timespan_map = {"1m": ("minute", 1), "5m": ("minute", 5),
                    "15m": ("minute", 15), "1h": ("hour", 1),
                    "1H": ("hour", 1), "1D": ("day", 1)}
    timespan, mult = timespan_map.get(interval, ("minute", 5))
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    url = (f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}"
            f"/range/{mult}/{timespan}/{start.date()}/{end.date()}"
            f"?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}")
    try:
        await _poly_gate.acquire()
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return pd.DataFrame()
            results = (r.json() or {}).get("results", [])
    except Exception as e:
        logger.warning(f"[Scanner] bars fetch failed for {ticker}: {e}")
        return pd.DataFrame()
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results)
    df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                              "c": "close", "v": "volume"})
    return df.set_index("timestamp")[["open", "high", "low", "close", "volume"]]


def _score(signal_metadata: dict) -> float:
    """Higher = better. Combines:
      • FVG size vs avg range (bigger fair-value gap = stronger imbalance)
      • Volume z-score at the signal bar (institutional footprint)
      • Displacement strength (body % of range)
    """
    score = 0.0
    score += float(signal_metadata.get("fvg_size_ratio", 0) or 0)
    score += float(signal_metadata.get("volume_zscore", 0) or 0) * 0.5
    score += float(signal_metadata.get("displacement_strength", 0) or 0) * 0.7
    return score


async def scan_universe(strategy_config: StrategyConfig,
                          universe: list[str],
                          top_k: int = 3,
                          progress_cb=None) -> list[ScannerHit]:
    """Run `strategy_config` against every ticker in `universe`. Return the
    top-K ranked hits."""
    hits: list[ScannerHit] = []
    n = len(universe)
    for i, ticker in enumerate(universe):
        try:
            bars = await _fetch_bars(ticker, lookback_days=5, interval="5m")
            if bars.empty or len(bars) < 30:
                continue
            # ICT strategy uses multi-TF dict
            bars_dict = {
                strategy_config.primary_timeframe: bars,
                strategy_config.execution_timeframe: bars,
            }
            if "1H" in (strategy_config.higher_timeframes or []):
                bars_dict["1H"] = bars.resample("1h").agg({
                    "open": "first", "high": "max", "low": "min",
                    "close": "last", "volume": "sum",
                }).dropna()

            strat = ICTStrategy(strategy_config, instrument=ticker)
            signal = strat.on_bar(bars_dict)
            if not signal or signal.signal == SignalType.NONE:
                continue

            md = dict(signal.metadata or {})
            spot = float(bars["close"].iloc[-1])
            direction = "long" if signal.signal == SignalType.LONG else "short"
            score = _score(md)
            reason = md.get("entry_reason") or f"{direction.upper()} signal on {ticker} — {md.get('fvg_type', 'FVG')} setup"
            hits.append(ScannerHit(
                ticker=ticker, direction=direction, score=score,
                spot=spot, bias=md.get("bias"), reason=reason,
                metadata=md,
            ))
        except Exception as e:
            logger.warning(f"[Scanner] {ticker} failed: {e}")
        if progress_cb:
            progress_cb((i + 1) / n)

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]
