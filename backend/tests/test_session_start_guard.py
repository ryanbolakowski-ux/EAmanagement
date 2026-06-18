"""Bug 1 (2026-06-05): a strategy must NOT open a position on a bar that
closed before its session was activated.

On session start the live data feed replays recent historical 1-min bars as
"live" bars. Before this fix each replayed bar was traded — jaceford12's SMT
session started 08:40:27 but entered 9 NQ trades stamped 08:31-08:43.

These are pure in-process tests (no DB, no network). They drive a real
PaperTrader / OptionsPaperTrader / LiveTrader with a stub that ALWAYS emits a
signal, so the ONLY thing that can suppress an entry is the new
`_session_started_at` guard.
"""
import asyncio
from datetime import datetime, timezone, timedelta

import pandas as pd
import pytest

from app.engines.strategy_engine.base_strategy import (
    BaseStrategy, StrategyConfig, TradeSignal, SignalType,
)


class _AlwaysLong(BaseStrategy):
    """Emits a LONG every bar. Risk controls always pass. So an entry is
    blocked ONLY by the session-start guard (or warmup)."""
    def __init__(self):
        super().__init__(StrategyConfig(name="always-long-test", instruments=["NQ"],
                                        max_contracts=1, risk_reward_ratio=2.0))

    def on_bar(self, bars):  # noqa: D401
        return TradeSignal(
            signal=SignalType.LONG, instrument="NQ",
            entry_price=20000.0, stop_loss=19990.0, take_profit=20020.0,
            contracts=1,
        )

    def on_tick(self, tick):  # abstract on BaseStrategy; unused by these tests
        return None

    def check_risk_controls(self) -> bool:
        return True


def _bar(ts: datetime, price: float = 20000.0) -> dict:
    return {"timestamp": pd.Timestamp(ts), "open": price, "high": price + 5,
            "low": price - 5, "close": price, "volume": 100}


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────── PaperTrader ────────────────────────────────────

def test_paper_skips_entry_on_pre_session_bar():
    """A bar 10 min before start() must NOT open a position (seed-only)."""
    from app.engines.paper_trading.paper_trader import PaperTrader

    async def go():
        trader = PaperTrader(_AlwaysLong(), "NQ", session_id="t-sess", user_id=None, strategy_id=None)
        await trader.start()
        stale_ts = trader._session_started_at - timedelta(minutes=10)
        # Buffer enough bars for the strategy, all stale; the guard must block.
        for i in range(5):
            await trader.on_bar("1m", _bar(stale_ts + timedelta(minutes=i)))
        return trader

    trader = _run(go())
    assert trader._position is None, "PaperTrader opened a position on a pre-session-start bar!"
    assert len(trader._completed_trades) == 0


def test_paper_allows_entry_on_fresh_bar():
    """A bar stamped 'now' (after start) MUST open a position — proves the
    guard is precise, not a blanket block of all entries."""
    from app.engines.paper_trading.paper_trader import PaperTrader

    async def go():
        trader = PaperTrader(_AlwaysLong(), "NQ", session_id="t-sess2", user_id=None, strategy_id=None)
        await trader.start()
        # Backdate session start past the 120s post-start settle window (a
        # separate, later guard) so this test isolates the session-start guard.
        trader._session_started_at = datetime.now(timezone.utc) - timedelta(seconds=200)
        fresh_ts = trader._session_started_at + timedelta(seconds=1)
        await trader.on_bar("1m", _bar(fresh_ts))
        return trader

    trader = _run(go())
    assert trader._position is not None, "PaperTrader failed to open on a fresh post-start bar!"
    assert trader._position.direction == "long"


def test_paper_grace_window_allows_mid_formation_bar():
    """A bar that closed 30s before start (inside the 90s grace) still trades —
    that's the bar forming when the user clicked activate."""
    from app.engines.paper_trading.paper_trader import PaperTrader

    async def go():
        trader = PaperTrader(_AlwaysLong(), "NQ", session_id="t-sess3", user_id=None, strategy_id=None)
        await trader.start()
        # Backdate session start past the 120s post-start settle window (a
        # separate, later guard) so this test isolates the session-start guard.
        trader._session_started_at = datetime.now(timezone.utc) - timedelta(seconds=200)
        within_grace = trader._session_started_at - timedelta(seconds=30)
        await trader.on_bar("1m", _bar(within_grace))
        return trader

    trader = _run(go())
    assert trader._position is not None, "Grace-window bar (30s < 90s) should still open a position!"


# ─────────────────────────── OptionsPaperTrader ─────────────────────────────

def test_options_paper_skips_signal_from_pre_session_bar():
    from app.engines.options.options_paper import OptionsPaperTrader

    trader = OptionsPaperTrader("SPY", chain=[], starting_balance=10_000.0)
    trader.start()
    stale = trader._session_started_at - timedelta(minutes=10)
    opened = trader.on_signal(side="long", spot=500.0, today=stale.date(), bar_ts=stale)
    assert opened is None, "OptionsPaperTrader opened on a pre-session-start signal!"
    assert trader._position is None


# ─────────────────────────── LiveTrader ─────────────────────────────────────

class _StubBroker:
    is_connected = True
    async def connect(self): return True
    async def disconnect(self): return None
    async def subscribe_quotes(self, *a, **k): return None
    async def subscribe_bars(self, *a, **k): return None


def test_live_skips_entry_on_pre_session_bar():
    """Real-money trader must never open on a replayed historical bar."""
    from app.engines.live_trading.live_trader import LiveTrader

    async def go():
        trader = LiveTrader(_AlwaysLong(), _StubBroker(), "NQ", session_id="t-live")
        # start() subscribes to the stub broker; that's fine.
        await trader.start()
        stale = trader._session_started_at - timedelta(minutes=10)
        await trader.on_bar("1m", _bar(stale))
        return trader

    trader = _run(go())
    assert trader._position is None, "LiveTrader opened a real position on a pre-session-start bar!"
