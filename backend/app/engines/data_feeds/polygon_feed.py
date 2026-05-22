"""
Polygon.io data feed for paid tier users.
Provides full historical intraday data (1m, 5m, 15m, etc.) going back years.
"""
import os
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger

POLYGON_API_KEY = os.getenv("POLYGON_API_KEY", "")

POLYGON_SYMBOLS = {
    "ES": "C:ESU2024",  # Will be dynamically resolved
    "NQ": "C:NQU2024",
    "RTY": "C:RTYU2024",
    "YM": "C:YMU2024",
}

# Use index tickers for historical data (more reliable on Polygon)
INDEX_TICKERS = {
    "ES": "I:SPX",
    "NQ": "I:NDX",
    "RTY": "I:RUT",
    "YM": "I:DJI",
}

# Futures continuous contracts
FUTURES_TICKERS = {
    "ES": "C:ES1!",
    "NQ": "C:NQ1!",
    "RTY": "C:RTY1!",
    "YM": "C:YM1!",
}

# Stocks Starter ETF proxies for futures (RTH session approximates futures price action).
ETF_PROXY_TICKERS = {
    "ES":  "SPY",
    "NQ":  "QQQ",
    "RTY": "IWM",
    "YM":  "DIA",
}

INTERVAL_MAP = {
    "1m": ("minute", 1),
    "2m": ("minute", 2),
    "3m": ("minute", 3),
    "5m": ("minute", 5),
    "15m": ("minute", 15),
    "30m": ("minute", 30),
    "1H": ("hour", 1),
    "1h": ("hour", 1),
    "4H": ("hour", 4),
    "4h": ("hour", 4),
    "1D": ("day", 1),
    "1d": ("day", 1),
}


async def fetch_polygon_data(
    instrument: str,
    start_date: datetime,
    end_date: datetime,
    interval: str = "15m",
) -> Optional[pd.DataFrame]:
    """Fetch historical data from Polygon.io REST API."""
    if not POLYGON_API_KEY:
        logger.warning("No POLYGON_API_KEY set")
        return None

    try:
        import httpx
    except ImportError:
        try:
            import subprocess
            subprocess.check_call(["pip", "install", "httpx", "--break-system-packages", "-q"])
            import httpx
        except Exception:
            logger.error("Could not install httpx for Polygon")
            return None

    if hasattr(start_date, "tzinfo") and start_date.tzinfo:
        start_date = start_date.replace(tzinfo=None)
    if hasattr(end_date, "tzinfo") and end_date.tzinfo:
        end_date = end_date.replace(tzinfo=None)

    timespan, multiplier = INTERVAL_MAP.get(interval, ("minute", 15))

    # Try ETF proxy first (Stocks Starter), then futures, then index
    tickers_to_try = []
    etf = ETF_PROXY_TICKERS.get(instrument.upper())
    if etf:
        tickers_to_try.append(etf)
    ft = FUTURES_TICKERS.get(instrument.upper())
    if ft:
        tickers_to_try.append(ft)
    idx = INDEX_TICKERS.get(instrument.upper())
    if idx:
        tickers_to_try.append(idx)

    for ticker in tickers_to_try:
        try:
            all_bars = []
            # Polygon limits results per request, so paginate
            chunk_start = start_date
            async with httpx.AsyncClient(timeout=30.0) as client:
                while chunk_start < end_date:
                    url = (
                        f"https://api.polygon.io/v2/aggs/ticker/{ticker}"
                        f"/range/{multiplier}/{timespan}"
                        f"/{chunk_start.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"
                        f"?adjusted=true&sort=asc&limit=50000&apiKey={POLYGON_API_KEY}"
                    )
                    resp = await client.get(url)
                    data = resp.json()

                    if data.get("status") == "ERROR" or data.get("resultsCount", 0) == 0:
                        logger.warning(f"Polygon returned no data for {ticker}: {data.get('error', data.get('status', 'unknown'))}")
                        break

                    results = data.get("results", [])
                    if not results:
                        break

                    all_bars.extend(results)

                    # Check if we got all the data or need to paginate
                    last_ts = results[-1]["t"]
                    last_dt = datetime.utcfromtimestamp(last_ts / 1000)
                    if last_dt >= end_date - timedelta(minutes=1):
                        break
                    if len(results) < 50000:
                        break
                    # Move start forward for next page
                    chunk_start = last_dt + timedelta(milliseconds=1)

            if not all_bars:
                continue

            df = pd.DataFrame(all_bars)
            df["timestamp"] = pd.to_datetime(df["t"], unit="ms", utc=True)
            df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
            df = df.set_index("timestamp")
            df = df[["open", "high", "low", "close", "volume"]].copy()
            df.index.name = "timestamp"
            df = df[~df.index.duplicated(keep='first')]
            df = df.sort_index()

            # Filter to exact date range
            start_ts = pd.Timestamp(start_date, tz="UTC")
            end_ts = pd.Timestamp(end_date, tz="UTC")
            df = df[(df.index >= start_ts) & (df.index <= end_ts)]

            logger.info(f"Polygon.io: {len(df)} bars for {instrument} ({ticker}) @ {multiplier}{timespan}")
            return df

        except Exception as e:
            logger.warning(f"Polygon failed for {ticker}: {e}")
            continue

    return None
