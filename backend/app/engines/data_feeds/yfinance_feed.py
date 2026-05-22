"""
Live data feed using yfinance polling.
Fetches the latest 1-minute bars every 60 seconds.
"""
import asyncio
import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
from loguru import logger

YAHOO_SYMBOLS = {
    'ES': 'ES=F',
    'NQ': 'NQ=F',
    'RTY': 'RTY=F',
    'YM': 'YM=F',
}


async def poll_latest_bars(instrument: str, timeframe: str = '1m', count: int = 5):
    """Fetch the most recent bars from Yahoo Finance."""
    symbol = YAHOO_SYMBOLS.get(instrument.upper(), f'{instrument}=F')
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period='1d', interval=timeframe)
        if df is None or df.empty:
            return []
        df = df.tail(count)
        bars = []
        for ts, row in df.iterrows():
            bars.append({
                'timestamp': ts.to_pydatetime(),
                'open': float(row['Open']),
                'high': float(row['High']),
                'low': float(row['Low']),
                'close': float(row['Close']),
                'volume': int(row['Volume']),
            })
        return bars
    except Exception as e:
        logger.error(f'[DataFeed] Error fetching {symbol}: {e}')
        return []
