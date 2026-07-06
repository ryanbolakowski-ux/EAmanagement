"""Tests for per-session paper-engine allocation (ALLOC-V1).

Covers the pure clamp/resolve helpers and the PaperTrader starting_balance
override (the mechanism that replaces the killed 6x session multiplier).
"""

import pytest

from app.engines.paper_trading.allocation import (
    DEFAULT_STARTING_BALANCE,
    MAX_STARTING_BALANCE,
    MIN_STARTING_BALANCE,
    clamp_starting_balance,
    resolve_starting_balance,
)


class TestClampStartingBalance:
    def test_in_range_passthrough(self):
        assert clamp_starting_balance(60_000) == 60_000.0

    def test_clamps_low(self):
        assert clamp_starting_balance(500) == MIN_STARTING_BALANCE
        assert clamp_starting_balance(-5) == MIN_STARTING_BALANCE

    def test_clamps_high(self):
        assert clamp_starting_balance(5_000_000) == MAX_STARTING_BALANCE

    def test_bounds_inclusive(self):
        assert clamp_starting_balance(1_000) == 1_000.0
        assert clamp_starting_balance(1_000_000) == 1_000_000.0

    def test_numeric_string_accepted(self):
        assert clamp_starting_balance("25000") == 25_000.0

    def test_rejects_non_numeric(self):
        with pytest.raises((TypeError, ValueError)):
            clamp_starting_balance("lots")
        with pytest.raises((TypeError, ValueError)):
            clamp_starting_balance(None)

    def test_rejects_nan_and_inf(self):
        with pytest.raises(ValueError):
            clamp_starting_balance(float("nan"))
        with pytest.raises(ValueError):
            clamp_starting_balance(float("inf"))


class TestResolveStartingBalance:
    def test_none_gives_default(self):
        assert resolve_starting_balance(None) == DEFAULT_STARTING_BALANCE

    def test_valid_passthrough(self):
        assert resolve_starting_balance(60_000.0) == 60_000.0

    def test_zero_or_negative_gives_default(self):
        assert resolve_starting_balance(0) == DEFAULT_STARTING_BALANCE
        assert resolve_starting_balance(-1) == DEFAULT_STARTING_BALANCE

    def test_garbage_gives_default(self):
        assert resolve_starting_balance("garbage") == DEFAULT_STARTING_BALANCE
        assert resolve_starting_balance(float("nan")) == DEFAULT_STARTING_BALANCE
        assert resolve_starting_balance(float("inf")) == DEFAULT_STARTING_BALANCE


class _DummyStrategy:
    """PaperTrader.__init__ only stores the strategy; nothing is called."""


def test_paper_trader_accepts_starting_balance_override():
    from app.engines.paper_trading.paper_trader import PaperTrader

    trader = PaperTrader(_DummyStrategy(), instrument="NQ", starting_balance=60_000)
    assert trader._starting_balance == 60_000.0
    assert trader._equity == 60_000.0


def test_paper_trader_default_balance_unchanged():
    from app.engines.paper_trading.paper_trader import PaperTrader

    trader = PaperTrader(_DummyStrategy(), instrument="ES")
    assert trader._starting_balance == 10_000.0
    assert trader._equity == 10_000.0
