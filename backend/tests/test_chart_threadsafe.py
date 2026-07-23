"""Thread-safety regression for the matplotlib trade-chart renderer.

Background: a bare exit-139 (SIGSEGV, no Python traceback) hit the backend at
the market open. RCA pinned it on generate_trade_chart() driving matplotlib's
STATEFUL, non-thread-safe pyplot API (plt.subplots / fig.savefig / plt.close)
from multiple asyncio.to_thread workers at once when several account-signal
watchers fired simultaneously. Concurrent mutation of matplotlib's global
figure-manager / Agg C-state corrupts it and crashes the whole process.

The fix is a module-level threading.Lock in app.services.trade_chart that
serialises the entire render. This test hammers the renderer from a
ThreadPoolExecutor(8) with many overlapping calls: without the lock this is a
segfault/corruption risk (and would take pytest down with it); with the lock
every render is serialised and returns valid PNG bytes.

Deterministic and email-free: it calls generate_trade_chart directly with a
fixed synthetic OHLC frame and never touches send_signal_email / Resend.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import pandas as pd

from app.services.trade_chart import generate_trade_chart


def _make_bars(n: int = 40) -> pd.DataFrame:
    """A small, strictly deterministic OHLC frame with a DatetimeIndex."""
    base = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
    idx = [base + timedelta(minutes=i) for i in range(n)]
    rows = []
    for i in range(n):
        o = 100.0 + (i % 5) * 0.5
        c = o + (0.4 if i % 2 == 0 else -0.3)
        h = max(o, c) + 0.6
        low = min(o, c) - 0.6
        rows.append({"open": o, "high": h, "low": low, "close": c, "volume": 1000 + i})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx))


def _render_one(i: int) -> bytes:
    """One valid long-trade render. Geometry: target > entry > stop."""
    return generate_trade_chart(
        symbol=f"NQ{i}",
        timeframe="1m",
        bars_df=_make_bars(),
        entry=100.0,
        stop=99.0,
        target=103.0,
        direction="long",
        key_levels={"vwap": 100.2, "prev_high": 102.5},
        stop_reason="prior swing low",
        target_reason="1:3 R",
        fire_time=datetime(2026, 7, 13, 13, 50, tzinfo=timezone.utc),
    )


def test_concurrent_renders_all_succeed():
    """8 worker threads, 64 overlapping renders — every one returns real PNG
    bytes and no worker raises. Serialised by _RENDER_LOCK; unsynchronised
    pyplot here would risk a process-killing SIGSEGV."""
    n_tasks = 64
    results: list[bytes] = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_render_one, i) for i in range(n_tasks)]
        for fut in as_completed(futures):
            # .result() re-raises any exception from the worker thread.
            png = fut.result()
            results.append(png)

    assert len(results) == n_tasks
    for png in results:
        assert isinstance(png, (bytes, bytearray)), type(png)
        assert len(png) > 0
        # PNG magic number — proves a real image came back, not a truncated buf.
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_lock_is_present():
    """The serialisation lock must exist and be a real lock — guards against a
    future refactor silently dropping it and reintroducing the race."""
    import threading

    from app.services import trade_chart

    assert hasattr(trade_chart, "_RENDER_LOCK")
    # threading.Lock() is a factory; instances are of this type.
    assert isinstance(trade_chart._RENDER_LOCK, type(threading.Lock()))
