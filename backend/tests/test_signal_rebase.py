"""SIGNAL-PRICE-TRUTH-V2 (owner rule 2026-07-13): emitted futures signal
prices must be REAL candle_cache prices, never the QQQ/SPY ETF-proxy scale.

Covers:
  - snap_to_tick per instrument (ES/NQ 0.25, YM 1.0, RTY 0.10, micros, default)
  - the exact 2026-07-13 10:45 ET Judas NQ short: proxy entry 29770.37 /
    stop 29820.37 / tp 29639.75, real 29698.75 -> 29698.75 / 29748.75 /
    29568.25 with the strategy's point offsets (+50.0 / -130.62) preserved
    within one tick
  - proxy_now translation base (offset = real_now - proxy_now)
  - > 1% drift hard-suppresses the signal before ANY persistence
  - rebased prices flow into the account_signals INSERT and the signal object
  - no real price available -> unchanged passthrough
  - ES already-aligned case (offset 0) unchanged

Run: pytest backend/tests/test_signal_rebase.py -q
"""
from __future__ import annotations

import asyncio

import pytest

from app.engines.account_signals import runner as rn
from app.engines.account_signals.runner import snap_to_tick, rebase_signal_prices
from app.engines.strategy_engine.base_strategy import TradeSignal, SignalType


# ── pure helpers ─────────────────────────────────────────────────────────────

def test_snap_to_tick_per_instrument():
    assert snap_to_tick(29568.13, "NQ") == 29568.25
    assert snap_to_tick(6234.30, "ES") == 6234.25
    assert snap_to_tick(44125.6, "YM") == 44126.0
    assert snap_to_tick(2245.13, "RTY") == 2245.1
    assert snap_to_tick(29568.13, "MNQ") == 29568.25
    assert snap_to_tick(6234.30, "MES") == 6234.25
    assert snap_to_tick(44125.6, "MYM") == 44126.0
    assert snap_to_tick(2245.13, "M2K") == 2245.1
    # unknown instrument -> default 0.25
    assert snap_to_tick(100.10, "ZZZ") == 100.0
    # exact tick prices pass through
    assert snap_to_tick(29698.75, "NQ") == 29698.75


def test_judas_1045_exact_case():
    """The 2026-07-13 14:45 UTC prod bug: emailed NQ entry 29770.37 while real
    NQ was 29698.75 (71.6pt error > the 50pt stop distance)."""
    entry, stop, tp = rebase_signal_prices(
        29770.37, 29820.37, 29639.75, real_now=29698.75, instrument="NQ")
    assert entry == 29698.75
    assert stop == 29748.75
    assert tp == 29568.25
    # point offsets the strategy intended: +50.0 stop, -130.62 target —
    # preserved within one tick (0.25) after snapping
    assert abs((stop - entry) - 50.0) <= 0.25
    assert abs((tp - entry) - (-130.62)) <= 0.25
    # every output is a valid 0.25-tick price (the proxy's .37 was not)
    for px in (entry, stop, tp):
        assert round(px / 0.25, 6) == int(round(px / 0.25))


def test_proxy_now_translation_base():
    """When the proxy series' current close is known, the offset is
    real_now - proxy_now (entry may be a past-bar FVG level, not 'now')."""
    # proxy now 29760.00, real now 29700.00 -> offset -60.00 on all prices
    entry, stop, tp = rebase_signal_prices(
        29770.25, 29820.25, 29639.75, real_now=29700.00, instrument="NQ",
        proxy_now=29760.00)
    assert entry == 29710.25
    assert stop == 29760.25
    assert tp == 29579.75


def test_es_already_aligned_unchanged():
    """ES priced straight from candle_cache: real == proxy, offset 0."""
    entry, stop, tp = rebase_signal_prices(
        6234.25, 6224.25, 6260.25, real_now=6234.25, instrument="ES")
    assert (entry, stop, tp) == (6234.25, 6224.25, 6260.25)


# ── _emit_signal wiring ──────────────────────────────────────────────────────

class _FakeResult:
    def fetchone(self):
        return None


class _FakeDB:
    def __init__(self, log):
        self._log = log

    async def execute(self, sql, params=None):
        self._log.append((str(sql), params))
        return _FakeResult()

    async def commit(self):
        pass


def _fake_session_factory(log):
    class _Ctx:
        async def __aenter__(self):
            return _FakeDB(log)

        async def __aexit__(self, *a):
            return False

    return lambda: _Ctx()


def _forbidden_session_factory():
    def _boom():
        raise AssertionError("DB touched — signal was NOT suppressed before persistence")

    return _boom


def _patch_gates(monkeypatch):
    """Bias + lunch gates pass; drift env vars at defaults."""
    import app.engines.bias_alignment as bias_mod
    import app.engines.lunch_window as lunch_mod

    async def _bias_ok(instrument, direction):
        return True, "test"

    monkeypatch.setattr(bias_mod, "direction_allowed", _bias_ok)
    monkeypatch.setattr(lunch_mod, "lunch_blocked",
                        lambda instrument, strategy_name=None: (False, "test"))
    monkeypatch.delenv("SIGNAL_MAX_ENTRY_DRIFT_PCT", raising=False)
    monkeypatch.delenv("SIGNAL_HARD_SUPPRESS_DRIFT_PCT", raising=False)
    monkeypatch.delenv("SIGNAL_DUP_COOLDOWN_MIN", raising=False)


def _judas_signal(entry=29770.37, stop=29820.37, tp=29639.75, metadata=None):
    return TradeSignal(signal=SignalType.SHORT, instrument="NQ",
                       entry_price=entry, stop_loss=stop, take_profit=tp,
                       metadata=metadata or {})


def _emit(signal, channels=None):
    return rn._emit_signal("w-test", "s-test", "u-test", "Test Acct",
                           channels or [], "Judas Swing", "NQ", signal,
                           "t@example.com", "trader")


def _inserted_signal_row(log):
    rows = [(sql, p) for sql, p in log
            if "INSERT INTO account_signals" in sql and p and "idem" in p]
    assert len(rows) == 1, f"expected exactly one signal INSERT, got {len(rows)}"
    return rows[0][1]


def test_rebased_prices_flow_downstream(monkeypatch):
    """The 0.24%-drift Judas case: entry/stop/tp are rebased BEFORE the
    account_signals INSERT, and the signal object (read by push + routing)
    is mutated too."""
    _patch_gates(monkeypatch)
    log = []
    monkeypatch.setattr(rn, "async_session_factory", _fake_session_factory(log))
    monkeypatch.setattr(rn, "_fresh_real_close", lambda inst: 29698.75)
    sig = _judas_signal()

    asyncio.run(_emit(sig))

    params = _inserted_signal_row(log)
    assert params["entry"] == 29698.75
    assert params["sl"] == 29748.75
    assert params["tp"] == 29568.25
    # signal object mutated — route_emitted_signal / push read these directly
    assert sig.entry_price == 29698.75
    assert sig.stop_loss == 29748.75
    assert sig.take_profit == 29568.25


def test_over_one_percent_drift_hard_suppresses(monkeypatch):
    """Proxy series untrustworthy (>1% off real): signal is NOT sent or
    routed, prices are NOT mutated, and exactly ONE audit row
    (status=suppressed, outcome_reason=price_truth_drift...) is written so
    the suppression is visible in the DB, not just container logs."""
    _patch_gates(monkeypatch)
    log = []
    monkeypatch.setattr(rn, "async_session_factory", _fake_session_factory(log))
    # real 29200 vs entry 29770.37 -> drift 1.95%
    monkeypatch.setattr(rn, "_fresh_real_close", lambda inst: 29200.00)
    sig = _judas_signal()

    asyncio.run(_emit(sig))

    inserts = [(q, p) for q, p in log if "INSERT INTO account_signals" in q]
    assert len(inserts) == 1, "hard-suppress must write exactly one audit row"
    _q, params = inserts[0]
    assert "suppressed" in _q
    assert str(params.get("reason", "")).startswith("price_truth_drift")
    # prices untouched — suppressed before any rebase/mutation
    assert sig.entry_price == 29770.37


def test_no_real_price_unchanged_passthrough(monkeypatch):
    """candle_cache empty (no real price): original prices flow through."""
    _patch_gates(monkeypatch)
    log = []
    monkeypatch.setattr(rn, "async_session_factory", _fake_session_factory(log))
    monkeypatch.setattr(rn, "_fresh_real_close", lambda inst: None)
    sig = _judas_signal()

    asyncio.run(_emit(sig))

    params = _inserted_signal_row(log)
    assert params["entry"] == 29770.37
    assert params["sl"] == 29820.37
    assert params["tp"] == 29639.75
    assert sig.entry_price == 29770.37


def test_proxy_now_from_chart_candles_used_as_base(monkeypatch):
    """When the strategy ships chart_candles, the LAST candle's close is the
    translation base: offset = real_now - proxy_now applied to entry/stop/tp
    (entry is an FVG level, not the current price)."""
    _patch_gates(monkeypatch)
    log = []
    monkeypatch.setattr(rn, "async_session_factory", _fake_session_factory(log))
    monkeypatch.setattr(rn, "_fresh_real_close", lambda inst: 29700.00)
    # proxy current close 29760.00 -> offset -60.00
    meta = {"chart_candles": [{"t": "2026-07-13T14:45:00+00:00",
                               "o": 29765.0, "h": 29775.0, "l": 29755.0,
                               "c": 29760.00}]}
    sig = _judas_signal(entry=29770.25, stop=29820.25, tp=29639.75, metadata=meta)

    asyncio.run(_emit(sig))

    params = _inserted_signal_row(log)
    assert params["entry"] == 29710.25
    assert params["sl"] == 29760.25
    assert params["tp"] == 29579.75
