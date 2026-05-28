"""
Read candle data from local PostgreSQL cache.
Aggregates 1m bars into any requested timeframe on the fly.
Supports both real futures data (from Databento) and ETF proxy data.
"""
import pandas as pd
from datetime import datetime
from typing import Optional
from sqlalchemy import text
from loguru import logger


TIMEFRAME_MINUTES = {
    "1m": 1, "2m": 2, "3m": 3, "4m": 4, "5m": 5,
    "10m": 10, "15m": 15, "30m": 30,
    "1H": 60, "1h": 60, "2H": 120, "2h": 120,
    "3H": 180, "3h": 180, "4H": 240, "4h": 240,
    "1D": 1440, "1d": 1440,
}

# Legacy ETF mappings (kept as fallback)
INSTRUMENT_TO_SYMBOL = {
    "ES": "SPY", "NQ": "QQQ", "RTY": "IWM", "YM": "DIA",
}

# Price scaling factors only used for legacy ETF data
PRICE_SCALE = {
    "ES": 8.3, "NQ": 31.0, "RTY": 8.1, "YM": 87.0,
}


async def fetch_from_cache(
    instrument: str,
    start_date: datetime,
    end_date: datetime,
    interval: str = "15m",
) -> Optional[pd.DataFrame]:
    """Fetch data from local candle_cache and aggregate to requested timeframe."""
    from app.database import async_session_factory

    inst = instrument.upper()

    if hasattr(start_date, "tzinfo") and start_date.tzinfo:
        start_date = start_date.replace(tzinfo=None)
    if hasattr(end_date, "tzinfo") and end_date.tzinfo:
        end_date = end_date.replace(tzinfo=None)

    try:
        async with async_session_factory() as db:
            # First try real futures data (symbol = instrument name like ES, NQ)
            result = await db.execute(
                text(
                    "SELECT timestamp, open, high, low, close, volume "
                    "FROM candle_cache "
                    "WHERE instrument = :inst AND timestamp >= :s AND timestamp <= :e "
                    "ORDER BY timestamp ASC"
                ),
                {"inst": inst, "s": start_date, "e": end_date}
            )
            rows = result.fetchall()

        if not rows:
            logger.warning(f"No cached data for {inst} between {start_date} and {end_date}")
            return None

        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
        df.index.name = "timestamp"

        # Check if this is real futures data or ETF proxy data
        first_price = df["open"].iloc[0]
        is_etf = False
        if inst == "ES" and first_price < 1000:
            is_etf = True
        elif inst == "NQ" and first_price < 5000:
            is_etf = True
        elif inst == "RTY" and first_price < 500:
            is_etf = True
        elif inst == "YM" and first_price < 10000:
            is_etf = True

        if is_etf:
            # Dynamic (futures/ETF) ratio — the old hardcoded PRICE_SCALE drifted
            # (NQ/QQQ was 31, now ~41 => ~25% wrong prices). proxy_scale fetches
            # the live ratio (cached 1h) and falls back to a recent constant.
            from app.engines.data_feeds.proxy_scale import get_proxy_scale
            scale = get_proxy_scale(inst)
            logger.info(f"[price-source] {inst}: ETF-proxy cache data (first_price={first_price:.2f}); scaling x{scale}")
            df["open"] = df["open"] * scale
            df["high"] = df["high"] * scale
            df["low"] = df["low"] * scale
            df["close"] = df["close"] * scale
        else:
            logger.info(f"[price-source] {inst}: real futures cache data (first_price={first_price:.2f}); no scaling")

        # Aggregate to requested timeframe
        minutes = TIMEFRAME_MINUTES.get(interval, 15)
        if minutes > 1:
            df = df.resample(f"{minutes}min").agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna()

        logger.info(f"Local cache: {len(df)} bars for {inst} @ {interval} | {df.index[0]} to {df.index[-1]}")
        return df

    except Exception as e:
        logger.error(f"Local cache fetch failed: {e}")
        return None
