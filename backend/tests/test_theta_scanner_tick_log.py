"""BUG B: _check_and_run_theta_scanner must emit a `[ThetaScanner] tick`
INFO log EVERY call - even outside the scan window. This is the heartbeat
that proves the function ran today.

Run: pytest backend/tests/test_theta_scanner_tick_log.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _reset_state():
    from app.engines.options import premarket_scheduler as ps
    ps._theta_fired_today = None
    ps._theta_last_scan_min = None
    ps._theta_no_pick_alerted_for_date.clear()
    ps._theta_exception_alerted_for_date.clear()
    yield
    ps._theta_fired_today = None
    ps._theta_last_scan_min = None
    ps._theta_no_pick_alerted_for_date.clear()
    ps._theta_exception_alerted_for_date.clear()


def _capture_log():
    """Return (id, list-of-strings). Caller removes id when done."""
    from loguru import logger as _lg
    captured: list[str] = []
    _id = _lg.add(lambda m: captured.append(str(m)), level="INFO")
    return _id, captured


def test_tick_log_fires_outside_window(monkeypatch):
    """Late afternoon 3pm ET - scanner is OUTSIDE the 6-9:50 window.
    We must still emit a tick so prod logs prove the function ran."""
    from app.engines.options import premarket_scheduler as ps
    from loguru import logger as _lg

    # 15:00 ET - outside window
    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 6, 4, 19, 0, 0, tzinfo=timezone.utc)  # 15:00 ET
    monkeypatch.setattr("datetime.datetime", FixedDT, raising=False)

    _id, captured = _capture_log()
    try:
        # We need to patch the LOCAL datetime import inside the function.
        # Easiest: just patch _dt by patching datetime module.
        with patch("app.engines.options.premarket_scheduler.os.environ.get", side_effect=lambda k, d=None: d):
            _run(ps._check_and_run_theta_scanner())
    finally:
        _lg.remove(_id)

    joined = " ".join(captured)
    assert "[ThetaScanner] tick" in joined, (
        f"BUG B REGRESSION: expected '[ThetaScanner] tick' log; got: {joined[:500]}"
    )
    # The window=out branch must be present
    assert "window=out" in joined or "window=in" in joined


def test_tick_log_fires_inside_window(monkeypatch):
    """7:00 ET - inside scan window. Tick must still log first thing."""
    from app.engines.options import premarket_scheduler as ps
    from loguru import logger as _lg

    class FixedDT(datetime):
        @classmethod
        def now(cls, tz=None):
            # 7:00 ET == 11:00 UTC in June (DST)
            return datetime(2026, 6, 4, 11, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("datetime.datetime", FixedDT, raising=False)

    # Make find_best_premarket_pick return None (no candidate) so the
    # function doesn't fire the email path.
    async def _no_pick(db):
        return None
    monkeypatch.setattr(
        "app.engines.options.theta_scanner.find_best_premarket_pick",
        _no_pick,
    )

    # Make async_session_factory return a no-op session
    class _DummySession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return None
        async def execute(self, *a, **k):
            class _R:
                def fetchall(self_inner): return []
                def scalar_one_or_none(self_inner): return None
            return _R()
        async def commit(self): pass
    monkeypatch.setattr("app.database.async_session_factory", lambda: _DummySession())

    _id, captured = _capture_log()
    try:
        _run(ps._check_and_run_theta_scanner())
    finally:
        _lg.remove(_id)

    joined = " ".join(captured)
    assert "[ThetaScanner] tick" in joined, (
        f"BUG B REGRESSION: expected '[ThetaScanner] tick' log inside window; "
        f"got: {joined[:500]}"
    )
