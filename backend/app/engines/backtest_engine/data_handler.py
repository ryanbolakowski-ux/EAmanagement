"""
Data handler for the backtesting engine.
Responsible for loading, resampling, and serving OHLCV data
across all requested timeframes.
"""
import pandas as pd
from pathlib import Path
from typing import Optional
from loguru import logger


TIMEFRAME_ALIASES = {
    "1m":  "1min",
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "1H":  "1h",
    "4H":  "4h",
    "1D":  "1D",
    "1W":  "1W",
}


class DataHandler:
    """
    Loads historical OHLCV data and provides multi-timeframe bar access.
    Data can come from:
      - Local CSV/Parquet files (for backtesting)
      - PostgreSQL (stored historical data)
      - External API (Polygon.io, Rithmic, etc.)
    """

    def __init__(self, instrument: str, base_timeframe: str = "1m"):
        self.instrument = instrument.upper()
        self.base_timeframe = base_timeframe
        self._base_data: Optional[pd.DataFrame] = None
        self._resampled: dict[str, pd.DataFrame] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Loading
    # ─────────────────────────────────────────────────────────────────────────

    def load_from_csv(self, filepath: str | Path):
        """
        Load 1m (or base TF) OHLCV data from a CSV file.
        Expected columns: timestamp, open, high, low, close, volume
        """
        df = pd.read_csv(filepath, parse_dates=["timestamp"])
        df = df.sort_values("timestamp").reset_index(drop=True)
        df = df.set_index("timestamp")
        df.index = pd.DatetimeIndex(df.index, tz="UTC")
        self._base_data = df
        logger.info(f"Loaded {len(df)} bars for {self.instrument} from {filepath}")

    def load_from_dataframe(self, df: pd.DataFrame):
        """Load directly from a DataFrame (from DB query or API)."""
        df = df.copy().sort_values("timestamp")
        if "timestamp" in df.columns:
            df = df.set_index("timestamp")
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        self._base_data = df
        logger.info(f"Loaded {len(df)} bars for {self.instrument}")

    # ─────────────────────────────────────────────────────────────────────────
    # Resampling
    # ─────────────────────────────────────────────────────────────────────────

    def build_timeframes(self, timeframes: list[str]):
        """Resample base data into all requested timeframes."""
        if self._base_data is None:
            raise ValueError("No base data loaded. Call load_from_csv or load_from_dataframe first.")

        # Always include base TF
        self._resampled[self.base_timeframe] = self._base_data.copy()

        for tf in timeframes:
            if tf == self.base_timeframe:
                continue
            alias = TIMEFRAME_ALIASES.get(tf, tf)
            resampled = self._base_data.resample(alias).agg({
                "open":   "first",
                "high":   "max",
                "low":    "min",
                "close":  "last",
                "volume": "sum",
            }).dropna()
            self._resampled[tf] = resampled
            logger.debug(f"Built {tf} TF: {len(resampled)} bars")

    # ─────────────────────────────────────────────────────────────────────────
    # Bar access (used by backtest runner to feed strategy)
    # ─────────────────────────────────────────────────────────────────────────

    def get_bars_up_to(self, timestamp: pd.Timestamp, timeframes: list[str], lookback: int = 200) -> dict[str, pd.DataFrame]:
        """Return a slice of bars up to (and including) `timestamp` for each TF."""
        result = {}
        for tf in timeframes:
            if tf not in self._resampled:
                continue
            df = self._resampled[tf]
            sliced = df[df.index <= timestamp].tail(lookback)
            result[tf] = sliced
        return result

    def get_timeframe_bars(self, timeframe: str) -> pd.DataFrame:
        if timeframe not in self._resampled:
            raise KeyError(f"Timeframe {timeframe} not built. Call build_timeframes first.")
        return self._resampled[timeframe]

    def date_range(self, timeframe: str = None) -> tuple[pd.Timestamp, pd.Timestamp]:
        tf = timeframe or self.base_timeframe
        df = self._resampled.get(tf, self._base_data)
        return df.index[0], df.index[-1]

    def filter_date_range(self, start: pd.Timestamp, end: pd.Timestamp):
        """Trim all loaded data to the given date range."""
        if self._base_data is not None:
            self._base_data = self._base_data[
                (self._base_data.index >= start) & (self._base_data.index <= end)
            ]
        for tf in self._resampled:
            self._resampled[tf] = self._resampled[tf][
                (self._resampled[tf].index >= start) & (self._resampled[tf].index <= end)
            ]
