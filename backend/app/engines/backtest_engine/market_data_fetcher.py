import pandas as pd
import numpy as np
import hashlib
from datetime import datetime, timedelta
from typing import Optional
from loguru import logger
from app.engines.data_feeds.tv_feed import _fetch_yfinance_robust, fetch_tv_data
from app.engines.data_feeds.polygon_feed import fetch_polygon_data
from app.engines.data_feeds.local_cache import fetch_from_cache

YAHOO_SYMBOLS = {
    "ES": "ES=F",
    "NQ": "NQ=F",
    "RTY": "RTY=F",
    "YM": "YM=F",
}

CONTINUOUS_SYMBOLS = {
    "ES": "^GSPC",
    "NQ": "^IXIC",
    "RTY": "^RUT",
    "YM": "^DJI",
}

# Historical price anchors by year for time-aware synthetic data
INSTRUMENT_PRICE_ANCHORS = {
    "ES": {2018: 2500, 2019: 3000, 2020: 3200, 2021: 4200, 2022: 4000, 2023: 4500, 2024: 5300, 2025: 5800, 2026: 5900},
    "NQ": {2018: 6500, 2019: 8000, 2020: 9000, 2021: 15000, 2022: 12000, 2023: 15000, 2024: 18000, 2025: 20000, 2026: 20500},
    "RTY": {2018: 1500, 2019: 1650, 2020: 1700, 2021: 2200, 2022: 1900, 2023: 2000, 2024: 2100, 2025: 2200, 2026: 2250},
    "YM": {2018: 24000, 2019: 27000, 2020: 28000, 2021: 35000, 2022: 33000, 2023: 35000, 2024: 38000, 2025: 42000, 2026: 43000},
}

INSTRUMENT_PARAMS = {
    "ES": {"daily_vol": 0.012, "tick_size": 0.25},
    "NQ": {"daily_vol": 0.015, "tick_size": 0.25},
    "RTY": {"daily_vol": 0.014, "tick_size": 0.10},
    "YM": {"daily_vol": 0.011, "tick_size": 1.0},
}


def _make_deterministic_seed(instrument: str, start_date: datetime) -> int:
    """Hash instrument + date for reproducible but varied synthetic data."""
    key = f"{instrument}:{start_date.strftime('%Y-%m-%d')}"
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def _get_time_aware_base_price(instrument: str, dt: datetime) -> float:
    """Interpolate base price from historical anchors."""
    anchors = INSTRUMENT_PRICE_ANCHORS.get(instrument.upper(), INSTRUMENT_PRICE_ANCHORS["ES"])
    year = dt.year + (dt.timetuple().tm_yday / 365.25)
    years = sorted(anchors.keys())

    if dt.year <= years[0]:
        return anchors[years[0]]
    if dt.year >= years[-1]:
        return anchors[years[-1]]

    for i in range(len(years) - 1):
        if years[i] <= dt.year <= years[i + 1]:
            frac = (year - years[i]) / (years[i + 1] - years[i])
            return anchors[years[i]] + frac * (anchors[years[i + 1]] - anchors[years[i]])
    return anchors[years[-1]]


async def fetch_futures_data(
    instrument: str,
    start_date: datetime,
    end_date: datetime,
    interval: str = "1m",
    use_polygon: bool = False,
) -> Optional[pd.DataFrame]:
    if hasattr(start_date, "tzinfo") and start_date.tzinfo:
        start_date = start_date.replace(tzinfo=None)
    if hasattr(end_date, "tzinfo") and end_date.tzinfo:
        end_date = end_date.replace(tzinfo=None)

    # Try local cache first (fastest, has 3 years of 1m data)
    try:
        df = await fetch_from_cache(instrument, start_date, end_date, interval)
        if df is not None and not df.empty:
            return df
    except Exception as e:
        logger.warning(f"Local cache failed: {e}")

    # Paid users get Polygon data as fallback
    if use_polygon:
        try:
            df = await fetch_polygon_data(instrument, start_date, end_date, interval)
            if df is not None and not df.empty:
                logger.info(f"Fetched {len(df)} bars for {instrument} ({interval}) from Polygon.io")
                return df
        except Exception as e:
            logger.warning(f"Polygon fetch failed: {e}")

    # Yahoo Finance fallback
    try:
        df = await _fetch_yfinance_robust(instrument, start_date, end_date, interval)
        if df is not None and not df.empty:
            logger.info(f"Fetched {len(df)} real bars for {instrument} ({interval}) from Yahoo Finance")
            return df
    except Exception as e:
        logger.warning(f"Yahoo fetch failed: {e}")

    logger.info(f"Falling back to synthetic data for {instrument}")
    return await _generate_realistic_data(instrument, start_date, end_date, interval)


def _map_interval(interval: str, date_range_days: int) -> str:
    interval_map = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                    "1H": "1h", "1h": "1h", "4H": "1h", "4h": "1h",
                    "1D": "1d", "1d": "1d"}
    yf_interval = interval_map.get(interval, "1d")
    if yf_interval == "1m" and date_range_days > 7:
        yf_interval = "5m" if date_range_days <= 60 else "15m"
    # Don't downgrade 5m/15m/30m - chunked fetching handles long ranges
    if yf_interval in ("1h",) and date_range_days > 730:
        yf_interval = "1d"
    return yf_interval


def _interval_to_minutes(interval: str) -> int:
    static = {
        "1m": 1, "5m": 5, "15m": 15, "30m": 30,
        "1H": 60, "1h": 60, "4H": 240, "4h": 240,
        "1D": 1440, "1d": 1440, "1W": 10080, "1w": 10080,
    }
    if interval in static:
        return static[interval]
    # Dynamic fallback: "90m" -> 90, "3h" -> 180
    try:
        if interval.endswith("m"):
            return int(interval[:-1])
        elif interval.endswith("h") or interval.endswith("H"):
            return int(interval[:-1]) * 60
        elif interval.endswith("d") or interval.endswith("D"):
            return int(interval[:-1]) * 1440
    except ValueError:
        pass
    return 15


async def _generate_realistic_data(instrument, start_date, end_date, interval="15m"):
    """Generate synthetic market data with realistic structure for ICT backtesting."""
    if hasattr(start_date, "tzinfo") and start_date.tzinfo is not None:
        start_date = start_date.replace(tzinfo=None)
    if hasattr(end_date, "tzinfo") and end_date.tzinfo is not None:
        end_date = end_date.replace(tzinfo=None)

    params = INSTRUMENT_PARAMS.get(instrument.upper(), INSTRUMENT_PARAMS["ES"])
    tick_size = params["tick_size"]
    daily_vol = params["daily_vol"]
    interval_minutes = _interval_to_minutes(interval)

    # Time-aware base price
    base_price = _get_time_aware_base_price(instrument, start_date)

    timestamps = _generate_trading_timestamps(start_date, end_date, interval_minutes)
    if not timestamps:
        return pd.DataFrame()

    n = len(timestamps)
    seed = _make_deterministic_seed(instrument, start_date)
    rng = np.random.default_rng(seed)

    price = base_price
    opens, highs, lows, closes, volumes = [], [], [], [], []

    # Slow drift for multi-day trend phases
    drift_per_bar = daily_vol * 0.003 * base_price / max(1, 1440 / interval_minutes)
    drift_direction = rng.choice([-1, 1])
    drift_switch_bars = int(rng.integers(50, 200))

    # Mean-reversion equilibrium
    equilibrium = base_price
    mean_rev_strength = 0.08

    for i in range(n):
        ts = timestamps[i]
        hour_utc = ts.hour

        # Switch drift direction periodically
        if i % drift_switch_bars == 0 and i > 0:
            drift_direction *= -1
            drift_switch_bars = int(rng.integers(40, 180))
        equilibrium += drift_direction * drift_per_bar

        # Session-based volatility multipliers
        vol_mult = 0.6
        vol_base = 10000
        if 8 <= hour_utc < 9:  # London open
            vol_mult = 1.8
            vol_base = 40000
        elif 9 <= hour_utc < 12:  # London session
            vol_mult = 1.2
            vol_base = 30000
        elif 13 <= hour_utc < 14:  # NY open
            vol_mult = 2.0
            vol_base = 60000
        elif 14 <= hour_utc < 16:  # NY morning
            vol_mult = 1.5
            vol_base = 50000
        elif 16 <= hour_utc < 18:  # NY afternoon
            vol_mult = 1.0
            vol_base = 35000
        elif 18 <= hour_utc < 21:  # NY PM / close
            vol_mult = 1.3
            vol_base = 40000

        bar_vol = daily_vol * vol_mult * np.sqrt(interval_minutes / (24 * 60))

        # Mean-reversion pull toward equilibrium
        reversion = mean_rev_strength * (equilibrium - price) / base_price
        move = rng.normal(reversion, bar_vol) * price

        # Displacement bars (~5% of active session bars) - creates FVGs
        is_displacement = False
        if vol_mult >= 1.2 and rng.random() < 0.05:
            is_displacement = True
            move *= rng.uniform(2.0, 3.5)

        o = _snap_to_tick(price, tick_size)
        c = _snap_to_tick(o + move, tick_size)

        if is_displacement:
            # Displacement: small wick on engulfing side, large body
            if c > o:  # Bullish displacement
                wick_up = abs(rng.exponential(max(0.01, bar_vol * price * 0.2)))
                wick_down = abs(rng.exponential(max(0.01, bar_vol * price * 0.1)))
            else:  # Bearish displacement
                wick_up = abs(rng.exponential(max(0.01, bar_vol * price * 0.1)))
                wick_down = abs(rng.exponential(max(0.01, bar_vol * price * 0.2)))
        else:
            wick_up = abs(rng.exponential(max(0.01, bar_vol * price * 0.5)))
            wick_down = abs(rng.exponential(max(0.01, bar_vol * price * 0.5)))

        h = _snap_to_tick(max(o, c) + wick_up, tick_size)
        l = _snap_to_tick(min(o, c) - wick_down, tick_size)

        # Ensure valid OHLC
        h = max(h, o, c)
        l = min(l, o, c)

        v = int(max(100, rng.exponential(vol_base) * vol_mult))

        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        volumes.append(v)

        price = c + rng.normal(0, bar_vol * price * 0.05)

    df = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes},
        index=pd.DatetimeIndex(timestamps, name="timestamp"),
    )
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    logger.info(f"Generated {len(df)} synthetic bars for {instrument} ({interval}) | base_price={base_price:.0f}")
    return df


def _snap_to_tick(price, tick_size):
    return round(round(price / tick_size) * tick_size, 2)


def _generate_trading_timestamps(start_date, end_date, interval_minutes):
    timestamps = []
    s = pd.Timestamp(start_date)
    e = pd.Timestamp(end_date)
    current = s.tz_localize("UTC") if s.tz is None else s.tz_convert("UTC")
    end = e.tz_localize("UTC") if e.tz is None else e.tz_convert("UTC")
    delta = timedelta(minutes=interval_minutes)
    while current < end:
        # Skip Saturday entirely
        if current.weekday() == 5:
            current += timedelta(days=1)
            current = current.replace(hour=23, minute=0, second=0, microsecond=0)
            continue
        # Sunday: only after 23:00 (futures open)
        if current.weekday() == 6 and current.hour < 23:
            current = current.replace(hour=23, minute=0, second=0, microsecond=0)
            continue
        # Friday after 22:00 - market closed
        if current.weekday() == 4 and current.hour >= 22:
            current += timedelta(days=2)
            current = current.replace(hour=23, minute=0, second=0, microsecond=0)
            continue
        # Daily maintenance halt 22:00-23:00
        if current.hour == 22:
            current = current.replace(hour=23, minute=0, second=0, microsecond=0)
            continue
        timestamps.append(current)
        current += delta
    return timestamps
