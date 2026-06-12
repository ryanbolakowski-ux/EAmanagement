"""ICTContext - the read-only bundle every ICT setup evaluator receives.

It is deliberately thin: it wraps exactly what ``ICTStrategy.on_bar`` already
has in scope (the timeframe->DataFrame bar dict, the instrument, the existing
``StrategyConfig``) plus a couple of convenience accessors, so porting a
strategy is a matter of *reading* from the context rather than re-plumbing the
engine. Nothing here mutates state or performs I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from app.engines.strategy_engine.base_strategy import StrategyConfig

ET = ZoneInfo("America/New_York")


@dataclass
class ICTContext:
    """Everything an :class:`~app.engines.ict.base.ICTSetup` needs to decide.

    Parameters
    ----------
    bars:
        ``{timeframe: OHLCV DataFrame}`` keyed exactly as the engine assembles
        it (e.g. ``{"1m": df, "15m": df, "1H": df}``). DataFrames carry a
        ``DatetimeIndex`` and ``[open, high, low, close, volume]`` columns.
    instrument:
        The symbol being evaluated (e.g. ``"ES"``).
    config:
        The existing :class:`StrategyConfig` (timeframes, sessions, RR, fvg
        ticks, ``rule_tree`` per-setup knobs live on it via ``getattr``).
    now_et:
        Timestamp of the current (latest primary) bar, localized to
        America/New_York. Falls back to wall-clock ET if no bars are present.
    correlated:
        Optional companion-instrument bar dict (for SMT divergence, a later
        step). ``None`` for single-instrument setups - which is all of them
        for now.
    """

    bars: dict[str, pd.DataFrame]
    instrument: str
    config: StrategyConfig
    now_et: datetime
    correlated: Optional[dict[str, pd.DataFrame]] = None
    # Free-form scratch space evaluators may use to stash intermediate state
    # (e.g. a detected sweep) without widening this dataclass per strategy.
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Thin accessors. These never raise on missing data; they return None /
    # empty so evaluators can guard cheaply, mirroring the engine's posture.
    # ------------------------------------------------------------------
    @property
    def rule_tree(self) -> dict:
        """The strategy's ``rule_tree`` JSON block (or ``{}``)."""
        return getattr(self.config, "rule_tree", None) or {}

    def tf(self, timeframe: str) -> Optional[pd.DataFrame]:
        """Return the bar DataFrame for ``timeframe`` (or ``None``)."""
        df = self.bars.get(timeframe)
        if df is None or len(df) == 0:
            return None
        return df

    @property
    def primary(self) -> Optional[pd.DataFrame]:
        """Bars for ``config.primary_timeframe``."""
        return self.tf(self.config.primary_timeframe)

    @property
    def execution(self) -> Optional[pd.DataFrame]:
        """Bars for ``config.execution_timeframe`` (falls back to primary)."""
        exec_df = self.tf(self.config.execution_timeframe)
        return exec_df if exec_df is not None else self.primary

    def higher(self) -> list[pd.DataFrame]:
        """The available higher-timeframe DataFrames, in config order."""
        out: list[pd.DataFrame] = []
        for tf in (self.config.higher_timeframes or []):
            df = self.tf(tf)
            if df is not None:
                out.append(df)
        return out

    @property
    def current_price(self) -> Optional[float]:
        """Latest close on the primary timeframe (or ``None``)."""
        df = self.primary
        if df is None:
            return None
        return float(df.iloc[-1]["close"])

    @classmethod
    def from_bars(
        cls,
        bars: dict[str, pd.DataFrame],
        instrument: str,
        config: StrategyConfig,
        correlated: Optional[dict[str, pd.DataFrame]] = None,
    ) -> "ICTContext":
        """Build a context from the same inputs ``on_bar`` receives.

        ``now_et`` is derived from the latest primary (or any available) bar so
        evaluators reason in exchange time; if no bars exist we fall back to the
        current wall-clock in ET.
        """
        now_et = _latest_ts_et(bars, config)
        return cls(
            bars=bars,
            instrument=instrument,
            config=config,
            now_et=now_et,
            correlated=correlated,
        )


def _latest_ts_et(bars: dict[str, pd.DataFrame], config: StrategyConfig) -> datetime:
    """Latest bar timestamp across the configured TFs, localized to ET."""
    candidates: list[pd.Timestamp] = []
    tf_order = [config.primary_timeframe, config.execution_timeframe]
    tf_order += list(config.higher_timeframes or [])
    for tf in tf_order:
        df = bars.get(tf)
        if df is not None and len(df) and isinstance(df.index, pd.DatetimeIndex):
            candidates.append(df.index[-1])
    if not candidates:
        return datetime.now(ET)
    ts = max(candidates)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.tz_convert(ET).to_pydatetime()
