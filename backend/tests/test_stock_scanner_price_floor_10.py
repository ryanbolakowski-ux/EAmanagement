"""Price floor: candidates priced below $10 are skipped; $10.01 are kept.

Run: pytest backend/tests/test_stock_scanner_price_floor_10.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def test_skips_candidate_below_price_floor(monkeypatch):
    """A $9.99 ticker should be filtered out by the price floor — even if
    its gap, volume, and rel_vol would otherwise produce a high score."""
    from app.engines.options import theta_scanner

    async def fake_snapshot():
        return [{
            "ticker": "MICRO",
            "day": {"c": 9.99, "v": 10_000_000},     # massive $-vol
            "prevDay": {"c": 8.5, "v": 2_000_000},
        }]
    monkeypatch.setattr(
        "app.engines.options.momentum_scanner._fetch_market_snapshot",
        fake_snapshot,
    )
    async def fake_cat(db, ticker):
        return 1.0, ""
    monkeypatch.setattr(theta_scanner, "_get_8k_catalyst", fake_cat)

    class _FakeDB: pass
    pick = _run(theta_scanner.find_best_premarket_pick(_FakeDB()))
    assert pick is None, f"$9.99 candidate should be rejected by price floor, got: {pick}"


def test_allows_candidate_at_or_above_price_floor(monkeypatch):
    """A $10.01 ticker should pass the price floor check."""
    from app.engines.options import theta_scanner

    async def fake_snapshot():
        # Make sure this also clears MIN_SCORE so we're testing the
        # price-floor pass, not the score-floor pass.
        return [{
            "ticker": "ABOVE",
            "day": {"c": 10.01, "v": 30_000_000},  # large $-vol
            "prevDay": {"c": 8.5, "v": 5_000_000}, # ~18% gap, 6x rel_vol
        }]
    monkeypatch.setattr(
        "app.engines.options.momentum_scanner._fetch_market_snapshot",
        fake_snapshot,
    )
    async def fake_cat(db, ticker):
        return 1.0, ""
    monkeypatch.setattr(theta_scanner, "_get_8k_catalyst", fake_cat)

    class _FR:
        async def setex(self, *a, **k): return True
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: _FR())

    class _FakeDB: pass
    pick = _run(theta_scanner.find_best_premarket_pick(_FakeDB()))
    assert pick is not None, "$10.01 candidate should pass price floor"
    assert pick["ticker"] == "ABOVE"
