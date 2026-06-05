"""Unit tests for start_options_paper (POST /api/v1/options-paper/sessions).

Pattern: we bypass FastAPI's DI and call the coroutine directly with
hand-built mocks. The handler does exactly two DB reads (strategy fetch,
duplicate-session check) and one write (TradeSession + commit + refresh),
all of which are easy to mock with AsyncMock.

We also mock the runner dispatch so no asyncio task ever spawns into a
real yfinance call during tests.

Run: pytest backend/tests/test_options_paper_start_session.py -v -p no:cacheprovider
"""
from __future__ import annotations

import sys
import types
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from app.api.routes.options_paper import (
    StartOptionsPaperRequest,
    start_options_paper,
)


# ─── helpers ────────────────────────────────────────────────────────────

class _FakeUser:
    def __init__(self, email="jaceford12@yahoo.com"):
        self.id = uuid.uuid4()
        self.email = email


class _FakeStrategy:
    """Minimal Strategy stand-in. Fields touched by start_options_paper:
    id, name, instruments, status, options_mode, and the duplicate-check
    relies on strat.id."""
    def __init__(self, name, instruments, options_mode=None, status="active"):
        self.id = uuid.uuid4()
        self.name = name
        self.instruments = instruments
        self.options_mode = options_mode
        self.status = status


def _make_db(strategy, existing_session=None):
    """Build an AsyncMock SQLAlchemy session with the two-execute pattern
    the handler uses. First execute → strategy lookup. Second execute →
    duplicate-active check. Subsequent operations are commit/refresh on
    the newly-created TradeSession."""
    db = AsyncMock()

    # First execute returns a result whose .scalar_one_or_none() = strategy
    strat_result = MagicMock()
    strat_result.scalar_one_or_none = MagicMock(return_value=strategy)

    # Second execute returns a result whose .scalar_one_or_none() = existing
    dup_result = MagicMock()
    dup_result.scalar_one_or_none = MagicMock(return_value=existing_session)

    db.execute = AsyncMock(side_effect=[strat_result, dup_result])
    db.add = MagicMock()
    db.commit = AsyncMock()

    # refresh stamps an id + started_at onto the session row
    async def _refresh(obj):
        obj.id = uuid.uuid4()
        from datetime import datetime, timezone
        obj.started_at = datetime.now(timezone.utc)
    db.refresh = AsyncMock(side_effect=_refresh)
    return db


@pytest.fixture(autouse=True)
def _stub_runner():
    """Replace the runner's start function with a no-op so nothing actually
    spawns yfinance work during tests. Patches the import target inside
    the handler's `try:` block."""
    fake_mod = types.ModuleType("app.engines.options.options_paper_runner")

    async def _noop(*args, **kwargs):
        return None
    fake_mod.start_options_paper_session = _noop
    sys.modules["app.engines.options.options_paper_runner"] = fake_mod
    yield
    sys.modules.pop("app.engines.options.options_paper_runner", None)


# ─── tests ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_session_with_stock_strategy_succeeds():
    """A vanilla stock strategy (e.g. SPY/QQQ instruments) → 201, session
    created, watch = strategy.instruments, underlying = first ticker."""
    strat = _FakeStrategy("Trend Pullback (Options)",
                           instruments=["SPY", "QQQ", "NVDA", "AAPL", "MSFT"])
    user = _FakeUser()
    db = _make_db(strat)
    req = StartOptionsPaperRequest(strategy_id=str(strat.id), underlying=None)

    resp = await start_options_paper(req, current_user=user, db=db)
    assert resp.is_active is True
    assert resp.strategy_name == "Trend Pullback (Options)"
    # Underlying should come from the strategy's instruments since no
    # explicit underlying was passed.
    assert resp.underlying == "SPY"
    db.commit.assert_awaited()
    db.add.assert_called_once()


@pytest.mark.asyncio
async def test_start_session_with_template_strategy_succeeds():
    """An empty-instruments template strategy (e.g. jace's 'Theta Scanner',
    '52-Week High Breakout', 'Low-Float Squeeze') → 201, session created,
    underlying picked from DEFAULT_WATCH[0] = 'SPY'."""
    strat = _FakeStrategy("Low-Float Squeeze", instruments=[])
    user = _FakeUser()
    db = _make_db(strat)
    req = StartOptionsPaperRequest(strategy_id=str(strat.id), underlying=None)

    resp = await start_options_paper(req, current_user=user, db=db)
    assert resp.is_active is True
    # DEFAULT_WATCH[0] = 'SPY'
    assert resp.underlying == "SPY"
    assert resp.strategy_name == "Low-Float Squeeze"


@pytest.mark.asyncio
async def test_start_session_with_futures_only_strategy_rejects():
    """ES/NQ-only strategy → 400 with the explicit 'futures-only' message
    pointing the user to the Futures Paper panel."""
    strat = _FakeStrategy("IOFED Precision Entry", instruments=["ES", "NQ"])
    user = _FakeUser()
    db = _make_db(strat)
    req = StartOptionsPaperRequest(strategy_id=str(strat.id))

    with pytest.raises(HTTPException) as exc:
        await start_options_paper(req, current_user=user, db=db)
    assert exc.value.status_code == 400
    assert "futures-only" in exc.value.detail
    assert "Futures Paper panel" in exc.value.detail
    # Critical: we must NOT have created a session row before bailing.
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_start_session_with_options_strategy_succeeds():
    """OCC-formatted instrument → classify_asset_class returns 'options' →
    accepted just like before, returns 201."""
    strat = _FakeStrategy("SPY Calls", instruments=["SPY240517C00500000"])
    user = _FakeUser()
    db = _make_db(strat)
    req = StartOptionsPaperRequest(strategy_id=str(strat.id), underlying="SPY")

    resp = await start_options_paper(req, current_user=user, db=db)
    assert resp.is_active is True
    assert resp.underlying == "SPY"


@pytest.mark.asyncio
async def test_start_session_with_options_mode_succeeds():
    """If options_mode is set (e.g. 'long_call'), the strategy is treated
    as options-capable regardless of instruments. We include this as a
    paranoia case — the new gate is purely class-based, but options_mode
    strategies usually have OCC-shaped or stock-shaped instruments."""
    strat = _FakeStrategy(
        "Vertical Spread (Options)",
        instruments=["SPY", "QQQ"],
        options_mode="vertical_call_debit",
    )
    user = _FakeUser()
    db = _make_db(strat)
    req = StartOptionsPaperRequest(strategy_id=str(strat.id))

    resp = await start_options_paper(req, current_user=user, db=db)
    assert resp.is_active is True
    assert resp.underlying == "SPY"


@pytest.mark.asyncio
async def test_duplicate_session_rejects():
    """An already-active session on the same (strategy, underlying) → 400
    with 'already running. Stop it first.' message."""
    strat = _FakeStrategy("Trend Pullback (Options)",
                           instruments=["SPY", "QQQ"])
    user = _FakeUser()
    existing = MagicMock()  # any truthy value triggers the duplicate guard
    db = _make_db(strat, existing_session=existing)
    req = StartOptionsPaperRequest(strategy_id=str(strat.id), underlying="SPY")

    with pytest.raises(HTTPException) as exc:
        await start_options_paper(req, current_user=user, db=db)
    assert exc.value.status_code == 400
    assert "already running" in exc.value.detail
    assert "SPY" in exc.value.detail
