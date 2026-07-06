"""Pure tests for the NY-lunch no-trade gate (app/engines/lunch_window.py).

All cases pass explicit `now_et` datetimes — no clock, no DB, no network.
Env is controlled via monkeypatch so the suite is order-independent.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.engines.lunch_window import lunch_blocked

ET = ZoneInfo("America/New_York")


def _et(hour, minute=0, second=0, year=2026, month=7, day=6):
    """Tz-aware ET datetime; default 2026-07-06 is a Monday (EDT)."""
    return datetime(year, month, day, hour, minute, second, tzinfo=ET)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("LUNCH_WINDOW_BLOCK", raising=False)
    monkeypatch.delenv("LUNCH_WINDOW_EXEMPT", raising=False)


def test_1059_et_allowed():
    blocked, why = lunch_blocked("NQ", now_et=_et(10, 59))
    assert blocked is False
    assert "outside" in why


def test_1100_et_blocked():
    blocked, why = lunch_blocked("NQ", now_et=_et(11, 0, 0))
    assert blocked is True
    assert "NY lunch 11:00-14:00 ET" in why


def test_135959_et_blocked():
    blocked, why = lunch_blocked("ES", now_et=_et(13, 59, 59))
    assert blocked is True
    assert "owner rule" in why


def test_1400_et_allowed():
    blocked, why = lunch_blocked("ES", now_et=_et(14, 0, 0))
    assert blocked is False
    assert "outside" in why


def test_stock_ticker_never_blocked():
    blocked, why = lunch_blocked("GPC", now_et=_et(12, 30))
    assert blocked is False
    assert "not a gated futures root" in why


def test_micro_mnq_blocked():
    blocked, why = lunch_blocked("MNQ", now_et=_et(12, 30))
    assert blocked is True


def test_lowercase_futures_root_blocked():
    blocked, _ = lunch_blocked("nq", now_et=_et(12, 0))
    assert blocked is True


def test_exempt_strategy_allowed_during_lunch(monkeypatch):
    monkeypatch.setenv("LUNCH_WINDOW_EXEMPT", "NY PM Reversal")
    blocked, why = lunch_blocked("NQ", strategy_name="NY PM Reversal",
                                 now_et=_et(12, 0))
    assert blocked is False
    assert "exempt" in why
    # A non-exempt strategy is still blocked with the exemption env set.
    blocked2, _ = lunch_blocked("NQ", strategy_name="FVG Continuation",
                                now_et=_et(12, 0))
    assert blocked2 is True


def test_kill_switch_allows(monkeypatch):
    monkeypatch.setenv("LUNCH_WINDOW_BLOCK", "0")
    blocked, why = lunch_blocked("NQ", now_et=_et(12, 0))
    assert blocked is False
    assert "disabled" in why


def test_weekend_evaluates_purely_on_time():
    # Saturday 2026-07-11 — no weekday special-casing: time alone decides.
    blocked, _ = lunch_blocked("NQ", now_et=_et(12, 0, 0, day=11))
    assert blocked is True
    blocked2, _ = lunch_blocked("NQ", now_et=_et(15, 0, 0, day=11))
    assert blocked2 is False


def test_unknown_or_none_instrument_not_gated():
    blocked, _ = lunch_blocked(None, now_et=_et(12, 0))
    assert blocked is False
    blocked2, _ = lunch_blocked("", now_et=_et(12, 0))
    assert blocked2 is False
    blocked3, _ = lunch_blocked("ZZTOP", now_et=_et(12, 0))
    assert blocked3 is False


def test_naive_datetime_taken_as_et():
    naive = datetime(2026, 7, 6, 12, 0, 0)  # no tzinfo -> treated as ET wall time
    blocked, _ = lunch_blocked("NQ", now_et=naive)
    assert blocked is True


def test_utc_datetime_converted_to_et():
    # 16:00 UTC on 2026-07-06 (EDT, UTC-4) == 12:00 ET -> blocked.
    utc = datetime(2026, 7, 6, 16, 0, 0, tzinfo=ZoneInfo("UTC"))
    blocked, _ = lunch_blocked("NQ", now_et=utc)
    assert blocked is True


# ── entry_guard INTEGRATION: the gates must return a real Decision ──────────
# (regression for the missing Decision.debug TypeError that made both gates
# silently fail open on every block attempt)

def test_entry_guard_bias_block_returns_decision(monkeypatch):
    import asyncio
    from app.engines import entry_guard as eg

    async def _deny(instrument, direction):
        return False, "daily bias BULLISH — shorts blocked on NQ (owner rule)"
    monkeypatch.setattr("app.engines.bias_alignment.direction_allowed", _deny)
    d = asyncio.run(eg.can_enter(session_id="t-sess", strategy_id="t-strat",
                                 instrument="NQ", direction="short"))
    assert d.allowed is False
    assert "BULLISH" in d.reason
    assert isinstance(d.debug, dict)


def test_entry_guard_lunch_block_returns_decision(monkeypatch):
    import asyncio
    from app.engines import entry_guard as eg

    async def _allow(instrument, direction):
        return True, "aligned"
    monkeypatch.setattr("app.engines.bias_alignment.direction_allowed", _allow)
    monkeypatch.setattr(
        "app.engines.lunch_window.lunch_blocked",
        lambda instrument, strategy_name=None, now_et=None:
            (True, "NY lunch 11:00-14:00 ET — new futures entries blocked (owner rule)"))
    monkeypatch.delenv("LUNCH_WINDOW_EXEMPT", raising=False)
    d = asyncio.run(eg.can_enter(session_id="t-sess", strategy_id="t-strat",
                                 instrument="NQ", direction="long"))
    assert d.allowed is False
    assert "lunch" in d.reason.lower()
    assert isinstance(d.debug, dict)
