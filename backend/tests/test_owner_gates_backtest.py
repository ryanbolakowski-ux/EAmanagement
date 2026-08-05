"""OWNER-GATES-BACKTEST-V1 — pure tests for the per-run A/B toggle that lets
Ryan run the SAME strategy+range WITH vs WITHOUT the live owner gates
(live-bias direction gate + NY-lunch no-trade window).

No DB / network: the as-of bias path is exercised with a fake session that
captures the SQL; the runner gate + normalization are pure functions.

Covers:
  * as-of bias is deterministic for a known date AND its query carries the
    upper time bound (look-ahead guard); the live (as_of=None) path does NOT.
  * normalize_bias mirrors the live gate (strong_*/unknown -> None).
  * the lunch ET-window helper at the 11:00 / 14:00 boundaries on BOTH an EDT
    (summer) and an EST (winter) date (DST), futures-roots only.
  * direction_allowed-equivalent skip logic (bullish blocks short, bearish
    blocks long, neutral/None block nothing), keyed by ET date not UTC.
  * default-off leaves BacktestConfig neutral (flag False, empty map).
"""
import asyncio
from datetime import datetime, timezone

import pandas as pd
import pytest

from app.engines.ict_bias import compute_ict_bias
from app.engines.bias_alignment import normalize_bias
from app.api.routes.dashboard import _compute_daily_bias
from app.engines.backtest_engine.backtest_runner import _owner_gate_skip, BacktestConfig


# ── helpers ──────────────────────────────────────────────────────────────────
def _uts(y, m, d, h, mi=0, s=0):
    """A tz-aware (UTC) pd.Timestamp, matching the runner's bar index."""
    return pd.Timestamp(datetime(y, m, d, h, mi, s, tzinfo=timezone.utc))


def _synthetic_rows(n_days=25, bars_per_day=30):
    """A deterministic 1m history: >=60 rows and >=21 daily bars so
    compute_ict_bias runs its real path (not the not-enough-data fallback)."""
    from datetime import timedelta
    rows = []
    base = datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)  # ~09:30 ET winter
    price = 5000.0
    for day in range(n_days):
        day_start = base + timedelta(days=day)
        for b in range(bars_per_day):
            ts = day_start + timedelta(minutes=b)  # plain datetime.datetime
            o = price
            price += 0.5
            rows.append((ts, o, price + 1.0, o - 1.0, price, 100.0))
    return rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDB:
    """Stand-in AsyncSession that returns canned rows and captures the SQL."""
    def __init__(self, rows):
        self._rows = rows
        self.calls = []  # (sql_text, params)

    async def execute(self, stmt, params):
        self.calls.append((str(stmt), params))
        return _FakeResult(self._rows)


_BIAS_LABELS = ("strong_bullish", "bullish", "neutral", "bearish", "strong_bearish")


# ── as-of bias: deterministic + look-ahead bounded ───────────────────────────
def test_as_of_bias_deterministic_for_known_date():
    rows = _synthetic_rows()
    as_of = datetime(2026, 1, 30, 14, 30, tzinfo=timezone.utc)
    db = _FakeDB(rows)
    r1 = asyncio.run(_compute_daily_bias(db, "ES", as_of=as_of))
    r2 = asyncio.run(_compute_daily_bias(db, "ES", as_of=as_of))
    assert r1 == r2, "same rows/date must yield the same bias (deterministic)"
    # as-of replay is exactly the pure engine on the queried rows
    assert r1 == compute_ict_bias([tuple(x) for x in rows], "ES")
    assert r1["bias"] in _BIAS_LABELS


def test_as_of_query_is_upper_bounded():
    """The look-ahead guard: the as-of path MUST bound timestamp <= :end with
    end == as_of, else 'historical' bias would pull future bars."""
    db = _FakeDB(_synthetic_rows())
    as_of = datetime(2026, 1, 30, 14, 30, tzinfo=timezone.utc)
    asyncio.run(_compute_daily_bias(db, "ES", as_of=as_of))
    sql, params = db.calls[-1]
    assert "timestamp <= :end" in sql
    assert params["end"] == as_of


def test_live_path_has_no_upper_bound():
    """as_of=None (live/dashboard) stays byte-identical: no upper bound, no
    'end' param."""
    db = _FakeDB(_synthetic_rows())
    asyncio.run(_compute_daily_bias(db, "ES"))
    sql, params = db.calls[-1]
    assert "<= :end" not in sql
    assert "end" not in params


# ── normalize_bias mirrors the live gate ─────────────────────────────────────
def test_normalize_bias_mirrors_live_gate():
    assert normalize_bias({"bias": "bullish"}) == "bullish"
    assert normalize_bias({"bias": "bearish"}) == "bearish"
    assert normalize_bias({"bias": "neutral"}) == "neutral"
    assert normalize_bias({"bias": "BULLISH"}) == "bullish"          # case-insensitive
    # strong trends collapse to None (load-bearing: strong_* -> NO gate)
    assert normalize_bias({"bias": "strong_bullish"}) is None
    assert normalize_bias({"bias": "strong_bearish"}) is None
    assert normalize_bias({"bias": "weird"}) is None
    assert normalize_bias({}) is None
    assert normalize_bias(None) is None
    # precedence: intraday_bias, then bias, then trend
    assert normalize_bias({"intraday_bias": "bearish", "bias": "bullish"}) == "bearish"
    assert normalize_bias({"trend": "bullish"}) == "bullish"
    assert normalize_bias({"trend": "strong_bullish"}) is None


# ── lunch ET-window helper: boundaries + DST ─────────────────────────────────
def test_lunch_gate_edt_boundaries():
    b = {}  # EDT (summer, UTC-4): 11:00 ET = 15:00 UTC
    assert _owner_gate_skip("NQ", "long", _uts(2026, 7, 6, 15, 0, 0), b) == "lunch"    # 11:00:00 ET
    assert _owner_gate_skip("NQ", "long", _uts(2026, 7, 6, 17, 59, 59), b) == "lunch"  # 13:59:59 ET
    assert _owner_gate_skip("NQ", "long", _uts(2026, 7, 6, 18, 0, 0), b) is None       # 14:00:00 ET
    assert _owner_gate_skip("NQ", "long", _uts(2026, 7, 6, 14, 59, 59), b) is None     # 10:59:59 ET


def test_lunch_gate_est_boundaries_dst():
    b = {}  # EST (winter, UTC-5): 11:00 ET = 16:00 UTC
    assert _owner_gate_skip("ES", "long", _uts(2026, 1, 6, 16, 0, 0), b) == "lunch"    # 11:00:00 ET
    assert _owner_gate_skip("ES", "long", _uts(2026, 1, 6, 17, 0, 0), b) == "lunch"    # 12:00:00 ET
    assert _owner_gate_skip("ES", "long", _uts(2026, 1, 6, 19, 0, 0), b) is None       # 14:00:00 ET
    assert _owner_gate_skip("ES", "long", _uts(2026, 1, 6, 15, 59, 59), b) is None     # 10:59:59 ET


def test_lunch_gate_futures_roots_only():
    mid = _uts(2026, 7, 6, 16, 0, 0)  # 12:00 ET
    assert _owner_gate_skip("AAPL", "long", mid, {}) is None        # non-futures never lunch-gated
    assert _owner_gate_skip("MNQ", "short", mid, {}) == "lunch"     # micro futures root gated
    assert _owner_gate_skip("nq", "long", mid, {}) == "lunch"       # case-insensitive root


# ── bias skip logic mirrors direction_allowed, keyed by ET date ──────────────
def test_bias_gate_mirrors_direction_allowed():
    ts = _uts(2026, 7, 6, 19, 0, 0)   # 15:00 ET — OUTSIDE lunch, isolates the bias gate
    key = "2026-07-06"
    assert _owner_gate_skip("NQ", "short", ts, {key: "bullish"}) == "bias"  # bullish blocks short
    assert _owner_gate_skip("NQ", "long", ts, {key: "bullish"}) is None     # bullish allows long
    assert _owner_gate_skip("ES", "long", ts, {key: "bearish"}) == "bias"   # bearish blocks long
    assert _owner_gate_skip("ES", "short", ts, {key: "bearish"}) is None    # bearish allows short
    for neutralish in ("neutral", None):
        assert _owner_gate_skip("NQ", "short", ts, {key: neutralish}) is None
        assert _owner_gate_skip("NQ", "long", ts, {key: neutralish}) is None
    assert _owner_gate_skip("NQ", "short", ts, {}) is None  # no bias for date -> no gate


def test_bias_date_key_uses_et_not_utc():
    # 2026-07-07 00:30 UTC == 2026-07-06 20:30 ET -> the ET trading date is 07-06.
    ts = _uts(2026, 7, 7, 0, 30, 0)
    assert _owner_gate_skip("NQ", "short", ts, {"2026-07-06": "bullish"}) == "bias"
    # a naive UTC-keyed map would MISS it — proving ET conversion is used.
    assert _owner_gate_skip("NQ", "short", ts, {"2026-07-07": "bullish"}) is None


# ── default-off is neutral ───────────────────────────────────────────────────
def test_default_off_config_is_neutral():
    cfg = BacktestConfig(
        instrument="ES",
        start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
        primary_timeframe="15m",
        all_timeframes=["15m"],
    )
    assert cfg.apply_owner_gates is False
    assert cfg.owner_bias_by_date == {}
