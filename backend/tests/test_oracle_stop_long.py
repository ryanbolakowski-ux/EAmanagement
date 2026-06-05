"""Oracle 5-min opening candle stop (LONG): given a 5-min bar with
low=$11.45, a long MOO entry should set stop = $11.44 (low - $0.01 to
sit just below the candle low). For pre-mkt confirmed entry, stop = the
pre-market session low.

Run: pytest backend/tests/test_oracle_stop_long.py -v -p no:cacheprovider
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest


def test_oracle_stop_long_from_5min_bar():
    """compute_oracle_stop_long(entry_method='MOO', oracle_candle={low: 11.45})
    must return (11.44, 'oracle-bar')."""
    from app.engines.options.premarket_scheduler import compute_oracle_stop_long
    stop, label = compute_oracle_stop_long(
        entry_method="MOO",
        premarket_low=None,
        oracle_candle={"h": 11.80, "l": 11.45, "o": 11.50, "c": 11.65},
        fallback_price=11.70,
    )
    assert stop == 11.44, f"expected $11.44, got ${stop}"
    assert label == "oracle-bar"


def test_oracle_stop_long_from_premarket_low_when_pre_mkt_method():
    """Pre-market confirmed entry — stop should be pre-market session low - $0.01."""
    from app.engines.options.premarket_scheduler import compute_oracle_stop_long
    stop, label = compute_oracle_stop_long(
        entry_method="pre-mkt",
        premarket_low=9.85,
        oracle_candle=None,
        fallback_price=10.20,
    )
    assert stop == 9.84
    assert label == "pre-mkt-low"


def test_oracle_stop_long_falls_back_to_3pct_when_no_data():
    """If Polygon failed and we have no bar/low, fall back to fallback_price * 0.97."""
    from app.engines.options.premarket_scheduler import compute_oracle_stop_long
    stop, label = compute_oracle_stop_long(
        entry_method="MOO",
        premarket_low=None,
        oracle_candle=None,
        fallback_price=100.0,
    )
    assert stop == 97.0, f"3% fallback expected, got {stop}"
    assert label == "fallback-3pct"


def test_oracle_stop_short_inverse():
    """Short entries use the opposite — high of the opening candle + $0.01."""
    from app.engines.options.premarket_scheduler import compute_oracle_stop_short
    stop, label = compute_oracle_stop_short(
        entry_method="MOO",
        premarket_high=None,
        oracle_candle={"h": 50.25, "l": 49.50, "o": 50.0, "c": 49.75},
        fallback_price=49.80,
    )
    assert stop == 50.26
    assert label == "oracle-bar"


def test_oracle_candle_picker_finds_first_rth_bar():
    """compute_oracle_5min_candle should return the bar opening at exactly
    09:30 ET (the ICT Oracle opening candle)."""
    from app.engines.options.premarket_scheduler import compute_oracle_5min_candle
    # 09:30 ET on June 5, 2026 = 13:30 UTC (EDT)
    oracle_t = int(datetime(2026, 6, 5, 13, 30, 0, tzinfo=timezone.utc).timestamp() * 1000)
    pre_t = int(datetime(2026, 6, 5, 13, 25, 0, tzinfo=timezone.utc).timestamp() * 1000)
    bars = [
        {"t": pre_t,    "o": 10.0, "h": 10.1, "l": 9.95, "c": 10.05, "v": 1000},
        {"t": oracle_t, "o": 10.05, "h": 10.30, "l": 9.98, "c": 10.20, "v": 100_000},
    ]
    result = compute_oracle_5min_candle(bars)
    assert result is not None
    assert result["l"] == 9.98
    assert result["h"] == 10.30
