"""Min-score floor: when no candidate scores >= 15.0, the scanner returns None.

Run: pytest backend/tests/test_stock_scanner_min_score_floor.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def low_score_snapshot():
    """Build a synthetic Polygon snapshot row that will score below 15."""
    return [
        {
            "ticker": "LOWA",
            "day": {"c": 15.0, "v": 500_000},        # $7.5M $-vol — barely passes liquidity
            "prevDay": {"c": 14.0, "v": 200_000},    # 7.1% gap, 2.5x rel_vol
        },
        {
            "ticker": "LOWB",
            "day": {"c": 20.0, "v": 400_000},        # $8M
            "prevDay": {"c": 18.5, "v": 180_000},    # ~8% gap, 2.2x rel_vol
        },
    ]


def test_returns_none_when_all_candidates_below_min_score(monkeypatch, low_score_snapshot):
    """All candidates score < 15 → find_best_premarket_pick returns None."""
    from app.engines.options import theta_scanner

    async def fake_snapshot():
        return low_score_snapshot
    monkeypatch.setattr(
        "app.engines.options.momentum_scanner._fetch_market_snapshot",
        fake_snapshot,
    )

    # No 8-K bumps. Stub the catalyst lookup.
    async def fake_cat(db, ticker):
        return 1.0, ""
    monkeypatch.setattr(theta_scanner, "_get_8k_catalyst", fake_cat)

    class _FakeDB: pass
    pick = _run(theta_scanner.find_best_premarket_pick(_FakeDB()))
    assert pick is None, f"expected None for sub-MIN_SCORE candidates, got: {pick}"


def test_returns_pick_when_top_candidate_clears_min_score(monkeypatch):
    """High-quality candidate (large gap + big volume) → returns dict."""
    from app.engines.options import theta_scanner

    async def fake_snapshot():
        # 22% gap × ln(30M)=17.2 × cat=1 × min(rel_vol=6, 10)=6 / 100
        # = 22 × 17.2 × 6 / 100 ≈ 22.7 — comfortably over MIN_SCORE=15.
        return [{
            "ticker": "STRONG",
            "day": {"c": 50.0, "v": 30_000_000},
            "prevDay": {"c": 41.0, "v": 5_000_000},
        }]
    monkeypatch.setattr(
        "app.engines.options.momentum_scanner._fetch_market_snapshot",
        fake_snapshot,
    )
    async def fake_cat(db, ticker):
        return 1.0, ""
    monkeypatch.setattr(theta_scanner, "_get_8k_catalyst", fake_cat)

    # Stub Redis persist so it doesn't actually try to connect
    async def _noop(*a, **k): return None
    class _FR:
        async def setex(self, *a, **k): return True
    monkeypatch.setattr("redis.asyncio.from_url", lambda *a, **k: _FR())

    class _FakeDB: pass
    pick = _run(theta_scanner.find_best_premarket_pick(_FakeDB()))
    assert pick is not None, "high-quality candidate should clear MIN_SCORE"
    assert pick["ticker"] == "STRONG"
    assert pick["score"] >= 15.0
