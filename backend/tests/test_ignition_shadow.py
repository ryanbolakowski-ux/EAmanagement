"""Unit tests for the IGNITION SHADOW scanner (shadow-only, owner approved).

Pure in-process tests: fake clock, fake quote feed, fake redis, fake persist.
No emails, no orders, no DB writes — persist is always stubbed.
"""
import asyncio
import re
import zoneinfo
from datetime import datetime, timedelta

import pytest

import app.engines.options.ignition_shadow as ign

ET = zoneinfo.ZoneInfo("America/New_York")


def _et(h, m, s):
    return datetime(2026, 7, 14, h, m, s, tzinfo=ET)  # a Tuesday


class FakeClock:
    """Deterministic ET clock advanced by the injected sleep()."""
    def __init__(self, start):
        self.now = start
        self.sleeps = 0

    def now_fn(self):
        return self.now

    async def sleep(self, seconds):
        self.sleeps += 1
        self.now = self.now + timedelta(seconds=seconds)


def make_feed(seqs):
    """Async quote fetcher: per-ticker price sequence; last value repeats."""
    idx = {}

    async def fetch(tk):
        seq = seqs.get(tk) or []
        i = idx.get(tk, 0)
        if i < len(seq):
            idx[tk] = i + 1
            return seq[i]
        return seq[-1] if seq else None

    return fetch


def make_persist(store):
    async def persist(row):
        store.append(row)
        return True
    return persist


def run_window(cands, seqs, start=None, **kw):
    clock = FakeClock(start or _et(9, 30, 20))
    rows = []
    summary = asyncio.run(ign.run_ignition_window(
        cands, fetch_price=make_feed(seqs), persist=make_persist(rows),
        now_fn=clock.now_fn, sleep=clock.sleep, **kw))
    return summary, rows, clock


# ── opening-range computation ──────────────────────────────────────────────
def test_or_computed_from_quote_samples():
    # 15s cadence from 09:30:20 -> OR samples at :20 :35 :50 1:05 1:20 (5),
    # then post-OR polls until 09:36.
    seqs = {"AAPL": [10.0, 10.2, 9.9, 10.1, 10.05] + [10.0] * 50}
    summary, rows, _ = run_window([{"ticker": "AAPL", "gap_pct": 4.2}], seqs)
    assert summary["or_low"]["AAPL"] == 9.9
    assert summary["or_high"]["AAPL"] == 10.2
    assert rows == []  # 10.0 never breaks 10.2 * 1.001


def test_or_ignores_failed_quotes():
    # None samples are skipped (fail-open); OR built from the good ones only
    seqs = {"AAPL": [None, 10.2, None, 9.9, None] + [10.0] * 50}
    summary, _, _ = run_window([{"ticker": "AAPL"}], seqs)
    assert summary["or_low"]["AAPL"] == 9.9
    assert summary["or_high"]["AAPL"] == 10.2


# ── break detection ────────────────────────────────────────────────────────
def test_break_fires_one_row_with_2r_target():
    or_prices = [10.0, 10.2, 9.9, 10.1, 10.05]
    seqs = {"AAPL": or_prices + [10.15, 10.25] + [10.30] * 50}
    summary, rows, _ = run_window(
        [{"ticker": "AAPL", "gap_pct": 4.2, "catalyst_reason": "8-K catalyst"}], seqs)
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "AAPL"
    assert row["direction"] == "long"
    assert row["entry"] == 10.25           # first price >= 10.2 * 1.001
    assert row["stop"] == 9.9              # OR-low
    assert row["target"] == pytest.approx(10.25 + 2 * (10.25 - 9.9))
    assert row["rr"] == 2.0
    assert row["score"] == 4.2             # score = gap_pct
    assert row["gap_pct"] == 4.2
    assert row["catalyst_reason"] == "8-K catalyst"
    assert row["shadow"] is True
    assert row["matched_strategy"] == "ignition_shadow"
    assert row["asset_type"] == "stocks"
    assert row["instrument_type"] == "watch_only"
    # only one row per ticker even though price stays above the trigger
    assert summary["fired"] == 1


def test_never_breaks_records_nothing_and_terminates():
    seqs = {"AAPL": [10.0, 10.2, 9.9] + [10.15] * 60}  # 10.15 < 10.2102 trigger
    summary, rows, clock = run_window([{"ticker": "AAPL"}], seqs)
    assert rows == []
    assert summary["fired"] == 0
    # loop actually ended at the 09:36 window close, not by hanging
    assert clock.now.hour == 9 and clock.now.minute >= 36


def test_break_exactly_at_threshold_fires():
    # trigger is >= OR-high * 1.001
    hi = 100.0
    trigger = hi * 1.001
    seqs = {"AAPL": [99.0, hi, 99.5, 99.8, 99.9] + [trigger] * 50}
    _, rows, _ = run_window([{"ticker": "AAPL"}], seqs)
    assert len(rows) == 1
    assert rows[0]["entry"] == pytest.approx(trigger)


def test_candidate_without_or_samples_never_arms():
    # all OR-window quotes fail -> no OR -> post-OR prices can never fire
    seqs = {"AAPL": [None, None, None, None, None] + [999.0] * 50}
    summary, rows, _ = run_window([{"ticker": "AAPL"}], seqs)
    assert rows == []
    assert "AAPL" not in summary["or_high"]


# ── daily row cap ──────────────────────────────────────────────────────────
def test_max_two_rows_per_day():
    or_p = [10.0] * 5
    breaking = or_p + [20.0] * 60
    seqs = {"AAA": breaking, "BBB": breaking, "CCC": breaking}
    cands = [{"ticker": t} for t in ("AAA", "BBB", "CCC")]
    summary, rows, _ = run_window(cands, seqs)
    assert len(rows) == 2
    assert summary["fired"] == 2
    assert {r["ticker"] for r in rows} == {"AAA", "BBB"}  # first two win


def test_candidates_capped_at_five():
    cands = ign.parse_candidates(
        [{"ticker": f"T{i}"} for i in range(9)])
    assert len(cands) == ign.MAX_CANDIDATES == 5


# ── candidate parsing tolerance ────────────────────────────────────────────
def test_parse_candidates_tolerant_shapes():
    assert ign.parse_candidates(None) == []
    assert ign.parse_candidates("") == []
    assert ign.parse_candidates("not json") == []
    assert ign.parse_candidates('["aapl", "TSLA"]') == [
        {"ticker": "AAPL"}, {"ticker": "TSLA"}]
    got = ign.parse_candidates('{"candidates": [{"symbol": "nvda", "gap_pct": 7.1}]}')
    assert got == [{"symbol": "nvda", "gap_pct": 7.1, "ticker": "NVDA"}]


# ── latch / env gate ───────────────────────────────────────────────────────
class FakeRedis:
    def __init__(self, latch_ok=True, candidates=None):
        self.latch_ok = latch_ok
        self.candidates = candidates
        self.set_keys = []

    async def set(self, key, value, ex=None, nx=None):
        self.set_keys.append((key, ex, nx))
        return self.latch_ok

    async def get(self, key):
        return self.candidates


def _freeze(monkeypatch, dt):
    monkeypatch.setattr(ign, "_now_et", lambda: dt)


def test_latch_already_taken_skips_run(monkeypatch):
    _freeze(monkeypatch, _et(9, 29, 50))
    r = FakeRedis(latch_ok=False)
    out = asyncio.run(ign.run_ignition_shadow_once(redis_client=r))
    assert out["status"] == "latched"
    key, ex, nx = r.set_keys[0]
    assert key == "theta:ignition:done:2026-07-14"
    assert nx is True and ex == 36 * 3600


def test_no_candidates_exits_after_latch(monkeypatch):
    _freeze(monkeypatch, _et(9, 29, 50))
    r = FakeRedis(latch_ok=True, candidates=None)
    out = asyncio.run(ign.run_ignition_shadow_once(redis_client=r))
    assert out["status"] == "no-candidates"


def test_weekend_never_runs(monkeypatch):
    sat = datetime(2026, 7, 18, 9, 31, 0, tzinfo=ET)
    _freeze(monkeypatch, sat)
    out = asyncio.run(ign.run_ignition_shadow_once(redis_client=FakeRedis()))
    assert out["status"] == "weekend"
    assert not ign.maybe_spawn_ignition_shadow(_now_et_fn=lambda: sat)


def test_env_gate_off(monkeypatch):
    monkeypatch.setenv("IGNITION_SHADOW_ENABLED", "0")
    out = asyncio.run(ign.run_ignition_shadow_once(redis_client=FakeRedis()))
    assert out["status"] == "disabled"
    assert not ign.maybe_spawn_ignition_shadow(_now_et_fn=lambda: _et(9, 31, 0))


def test_spawn_once_per_day_inside_window(monkeypatch):
    ign._spawned_dates.clear()
    calls = []

    async def fake_once(**kw):
        calls.append(1)
        return {"status": "done"}

    monkeypatch.setattr(ign, "run_ignition_shadow_once", fake_once)

    async def main():
        # outside the 09:29-09:36 window -> no spawn
        assert not ign.maybe_spawn_ignition_shadow(_now_et_fn=lambda: _et(9, 20, 0))
        assert not ign.maybe_spawn_ignition_shadow(_now_et_fn=lambda: _et(9, 36, 0))
        # inside -> spawn exactly once per day per process
        assert ign.maybe_spawn_ignition_shadow(_now_et_fn=lambda: _et(9, 29, 30))
        assert not ign.maybe_spawn_ignition_shadow(_now_et_fn=lambda: _et(9, 31, 0))
        await asyncio.sleep(0)  # let the spawned task run

    asyncio.run(main())
    assert calls == [1]
    ign._spawned_dates.clear()


# ── row shape vs the reference shadow-scan INSERT ──────────────────────────
def _insert_columns(source: str) -> list[set]:
    outs = []
    for m in re.finditer(r"INSERT INTO email_signals_history\s*\(([^)]*)\)", source):
        outs.append({c.strip() for c in m.group(1).split(",") if c.strip()})
    return outs


def test_insert_columns_match_daily_shadow_reference():
    import inspect
    import app.engines.scanner.shadow as ref_mod
    ign_cols = _insert_columns(inspect.getsource(ign))
    ref_cols = _insert_columns(inspect.getsource(ref_mod))
    assert len(ign_cols) == 1, "ignition module must have exactly one INSERT"
    assert ref_cols, "reference daily shadow INSERT not found"
    # identical column list -> the resolver treats both cohorts identically
    assert ign_cols[0] == ref_cols[0]


def test_build_shadow_row_covers_all_bind_params():
    import inspect
    src = inspect.getsource(ign.persist_shadow_row)
    binds = set(re.findall(r'row\["(\w+)"\]', src))
    assert binds, "no row[...] binds found in persist_shadow_row source"
    row = ign.build_shadow_row({"ticker": "AAPL", "gap_pct": 3.0}, entry=10.0, stop=9.5)
    for k in binds:
        assert k in row, f"persist binds row[{k!r}] but build_shadow_row omits it"
    # defensive stop guard: stop >= entry gets clamped below entry
    bad = ign.build_shadow_row({"ticker": "X"}, entry=10.0, stop=11.0)
    assert bad["stop"] < bad["entry"]
    assert bad["target"] > bad["entry"]
