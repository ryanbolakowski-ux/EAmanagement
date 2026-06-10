"""ICTSetup - the abstract base every per-strategy evaluator implements.

A setup composes the shared ICT primitives in the specific sequence that
*defines* one named strategy and returns the **same** ``TradeSignal`` the
engine already uses, so nothing downstream (paper/live/backtest/account-signals
/email) has to change.

This module contains ONLY the contract + generic, strategy-agnostic helpers
(structural stop, RR target, min-RR gate). It deliberately contains no
specific strategy's logic - those live in ``engines/ict/setups/`` in later
build steps.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd

from app.engines.ict.context import ICTContext
from app.engines.strategy_engine.base_strategy import TradeSignal
from app.engines.strategy_engine.indicators import (
    find_swing_highs,
    find_swing_lows,
    get_tick_size,
)


class ICTSetup(ABC):
    """Base class for a single named ICT strategy's evaluator.

    Subclasses implement :meth:`evaluate`. They receive an :class:`ICTContext`
    and return a :class:`TradeSignal` to open a trade or ``None`` to stand
    aside. Subclasses should be small and side-effect free; the registry owns
    instantiation and the engine owns dispatch + fallback.
    """

    #: Optional human/string id; the registry sets/uses the registered name.
    name: str = "ict_setup"

    @abstractmethod
    def evaluate(self, ctx: ICTContext) -> Optional[TradeSignal]:
        """Return a :class:`TradeSignal` or ``None``. Must not raise on normal
        missing-data conditions - guard and return ``None`` instead."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared helpers reused across setups. Pure / stateless given inputs.
    # ------------------------------------------------------------------
    @staticmethod
    def _stop_from_structure(
        df: pd.DataFrame,
        direction: str,
        instrument: str,
        lookback: int = 3,
        buffer_ticks: float = 2.0,
        anchor_level: Optional[float] = None,
    ) -> Optional[float]:
        """Structural stop-loss: just beyond the protective swing.

        For a **long**, the stop sits ``buffer_ticks`` below the most recent
        swing low (or below ``anchor_level`` when given - e.g. the swept
        extreme that defines the setup). For a **short**, mirror above the
        swing high. Returns ``None`` if no structure is available.
        """
        tick = get_tick_size(instrument)
        buf = buffer_ticks * tick
        d = (direction or "").lower()

        if anchor_level is not None:
            return float(anchor_level - buf) if d in ("long", "bullish") else float(anchor_level + buf)

        if df is None or len(df) == 0:
            return None

        if d in ("long", "bullish"):
            lows = find_swing_lows(df, lookback)
            if not lows:
                return float(df["low"].min() - buf)
            return float(lows[-1].price - buf)
        else:
            highs = find_swing_highs(df, lookback)
            if not highs:
                return float(df["high"].max() + buf)
            return float(highs[-1].price + buf)

    @staticmethod
    def _target_from_rr(
        entry: float,
        stop_loss: float,
        direction: str,
        rr: float,
    ) -> float:
        """Take-profit at a fixed reward:risk multiple of the stop distance."""
        risk = abs(entry - stop_loss)
        reward = risk * rr
        if (direction or "").lower() in ("long", "bullish"):
            return float(entry + reward)
        return float(entry - reward)

    @staticmethod
    def _min_rr_ok(
        entry: float,
        stop_loss: float,
        take_profit: float,
        min_rr: float,
    ) -> bool:
        """True iff the realized reward:risk of the bracket meets ``min_rr``.

        Returns ``False`` for a degenerate (zero-distance) stop so callers
        never divide by zero or emit a no-risk trade.
        """
        risk = abs(entry - stop_loss)
        if risk <= 0:
            return False
        reward = abs(take_profit - entry)
        return (reward / risk) >= float(min_rr)
