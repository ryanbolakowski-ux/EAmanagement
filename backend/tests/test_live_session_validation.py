"""Unit tests for assert_broker_supports_strategy.

This is the validation gate the live-trading POST /sessions endpoint runs
after fetching the strategy and broker account. The helper is module-level
and pure (no DB), so we can exercise it with lightweight stand-ins.
"""

import pytest
from fastapi import HTTPException

from app.api.routes.live_trading import assert_broker_supports_strategy


class _FakeStrategy:
    """Minimal duck-typed stand-in for the Strategy ORM row."""
    def __init__(self, name, instruments):
        self.name = name
        self.instruments = instruments


class _FakeBrokerAccount:
    """Minimal duck-typed stand-in for the BrokerAccount ORM row."""
    def __init__(self, broker):
        self.broker = broker


# ── happy paths ────────────────────────────────────────────────────────

def test_futures_strategy_to_tradovate_passes():
    """ES on Tradovate — the canonical happy path."""
    s = _FakeStrategy("ES Liquidity Sweep", ["ES", "NQ"])
    a = _FakeBrokerAccount("tradovate")
    # Should NOT raise.
    assert assert_broker_supports_strategy(s, a) is None


def test_stock_strategy_to_tradier_passes():
    """SPY on Tradier — also happy path."""
    s = _FakeStrategy("Tech Momentum", ["SPY", "NVDA"])
    a = _FakeBrokerAccount("tradier")
    assert assert_broker_supports_strategy(s, a) is None


def test_options_strategy_to_tradier_passes():
    """OCC option on Tradier — happy path for the options flow."""
    s = _FakeStrategy("SPY Calls", ["SPY240517C00500000"])
    a = _FakeBrokerAccount("tradier")
    assert assert_broker_supports_strategy(s, a) is None


def test_case_insensitive_broker_name():
    """Broker name is normalized — uppercase/mixed-case must still work."""
    s = _FakeStrategy("ES", ["ES"])
    a = _FakeBrokerAccount("TRADOVATE")
    assert assert_broker_supports_strategy(s, a) is None


# ── rejection paths ────────────────────────────────────────────────────

def test_futures_strategy_to_tradier_raises_400():
    """The bug we're fixing: ES strategy + Tradier (stock/options only)
    must be rejected with a 400, never silently dispatched."""
    s = _FakeStrategy("ES Liquidity Sweep", ["ES", "NQ"])
    a = _FakeBrokerAccount("tradier")
    with pytest.raises(HTTPException) as exc:
        assert_broker_supports_strategy(s, a)
    assert exc.value.status_code == 400
    msg = exc.value.detail
    assert "futures" in msg
    assert "tradier" in msg
    assert "ES Liquidity Sweep" in msg


def test_stock_strategy_to_tradovate_raises_400():
    """SPY strategy on a Tradovate (futures-only) account is rejected."""
    s = _FakeStrategy("Momentum", ["SPY"])
    a = _FakeBrokerAccount("tradovate")
    with pytest.raises(HTTPException) as exc:
        assert_broker_supports_strategy(s, a)
    assert exc.value.status_code == 400
    assert "stock" in exc.value.detail
    assert "tradovate" in exc.value.detail


def test_options_strategy_to_tradovate_raises_400():
    """Options strategy on Tradovate — Tradovate is futures-only."""
    s = _FakeStrategy("Iron Condor", ["SPY240517C00500000"])
    a = _FakeBrokerAccount("tradovate")
    with pytest.raises(HTTPException) as exc:
        assert_broker_supports_strategy(s, a)
    assert exc.value.status_code == 400


def test_template_strategy_with_empty_instruments_raises_400():
    """An undeployable template (instruments==[]) must never reach the
    runner — the error tells the user to add a symbol."""
    s = _FakeStrategy("My Draft", [])
    a = _FakeBrokerAccount("tradier")
    with pytest.raises(HTTPException) as exc:
        assert_broker_supports_strategy(s, a)
    assert exc.value.status_code == 400
    assert "no instruments" in exc.value.detail.lower()


def test_unknown_broker_raises_400():
    """An ES strategy + an unknown broker → 400 with a clear message."""
    s = _FakeStrategy("ES", ["ES"])
    a = _FakeBrokerAccount("robinhood")
    with pytest.raises(HTTPException) as exc:
        assert_broker_supports_strategy(s, a)
    assert exc.value.status_code == 400
    # Either we hit the "robinhood doesn't support futures" path
    # (because the unknown broker → empty supported list).
    assert "robinhood" in exc.value.detail
