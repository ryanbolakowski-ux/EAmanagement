"""TRACK fmp-self-sufficiency — the FMP EOD volume snapshot.

Fully mocked (no live FMP, no real Postgres, no real Redis). Coverage:
  • save_snapshot / load_prev_session_map round-trip through the SQL seam
    (a FakeDB emulates the fmp_eod_snapshot table statement-by-statement).
  • Idempotent per-date writes (re-run replaces, never duplicates).
  • 5-session retention: older session dates are pruned on save.
  • 'Most recent COMPLETED session' semantics: load only returns dates
    strictly BEFORE today; no prior session → {} (the universe then bridges).
  • capture_eod_snapshot: 4 FMP requests (3 movers + 1 screener), screener
    rows keep (close, volume), below-sweep movers keep (close, 0) — unknown
    volume is never fabricated; empty key/all-fail → 0 rows, no raise.
  • Scheduler hook: env gate (EOD_SNAPSHOT_ENABLED default on), weekday +
    >=16:15 ET time gate, once-per-day Redis SETNX latch, redis-down → skip,
    capture failure swallowed.

Run: pytest backend/tests/test_fmp_eod_snapshot.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

import app.engines.data_feeds.fmp_eod_snapshot as es
import app.engines.data_feeds.fmp_universe as fu


# ── FakeDB: an in-memory fmp_eod_snapshot table behind the SQL seam ─────────
class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Emulates exactly the statements fmp_eod_snapshot issues against the
    table. Store shape: {session_date: {symbol: (close, volume)}}."""

    def __init__(self, store):
        self.store = store
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def commit(self):
        self.commits += 1

    async def execute(self, stmt, params=None):
        q = " ".join(str(stmt).split()).upper()
        if q.startswith("CREATE TABLE"):
            return _FakeResult()
        if q.startswith("DELETE FROM FMP_EOD_SNAPSHOT WHERE SESSION_DATE ="):
            self.store.pop(params["d"], None)
            return _FakeResult()
        if q.startswith("DELETE FROM FMP_EOD_SNAPSHOT WHERE SESSION_DATE <"):
            for d in [d for d in self.store if d < params["cut"]]:
                del self.store[d]
            return _FakeResult()
        if q.startswith("INSERT INTO FMP_EOD_SNAPSHOT"):
            plist = params if isinstance(params, list) else [params]
            for p in plist:
                self.store.setdefault(p["d"], {})[p["s"]] = (p["c"], p["v"])
            return _FakeResult()
        if q.startswith("SELECT DISTINCT SESSION_DATE"):
            return _FakeResult([(d,) for d in sorted(self.store, reverse=True)])
        if q.startswith("SELECT MAX(SESSION_DATE)"):
            prior = [d for d in self.store if d < params["today"]]
            return _FakeResult([(max(prior),)] if prior else [(None,)])
        if q.startswith("SELECT SYMBOL, CLOSE, VOLUME"):
            rows = self.store.get(params["d"], {})
            return _FakeResult([(s, c, v) for s, (c, v) in rows.items()])
        raise AssertionError(f"unexpected SQL against the fake table: {q}")


@pytest.fixture()
def fake_store(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(es, "_session_factory", lambda: (lambda: _FakeDB(store)))
    return store


# ── storage: write / read / retention ───────────────────────────────────────
def test_save_and_load_roundtrip(fake_store):
    n = asyncio.run(es.save_snapshot(
        "2026-07-02", {"NVDA": (194.83, 142_385_548), "clro": (6.48, 88_538_476)}))
    assert n == 2
    assert fake_store["2026-07-02"]["CLRO"] == (6.48, 88_538_476)  # upcased

    prev = asyncio.run(es.load_prev_session_map("2026-07-03"))
    assert prev == {"NVDA": {"c": 194.83, "v": 142_385_548.0},
                    "CLRO": {"c": 6.48, "v": 88_538_476.0}}


def test_save_skips_garbage_and_is_idempotent_per_date(fake_store):
    quotes = {"AAA": (10.0, 1_000_000), "": (5.0, 1), "BAD": ("nope", 1),
              "ZERO": (0.0, 9)}
    assert asyncio.run(es.save_snapshot("2026-07-02", quotes)) == 1
    # re-run for the SAME date replaces instead of duplicating
    assert asyncio.run(es.save_snapshot("2026-07-02", {"AAA": (11.0, 2_000_000)})) == 1
    assert fake_store["2026-07-02"] == {"AAA": (11.0, 2_000_000)}
    # nothing valid → 0 written, store untouched
    assert asyncio.run(es.save_snapshot("2026-07-03", {"": (1.0, 1)})) == 0
    assert "2026-07-03" not in fake_store


def test_retention_keeps_newest_five_sessions(fake_store):
    days = [f"2026-06-{d:02d}" for d in (22, 23, 24, 25, 26, 29, 30)]
    for d in days:
        asyncio.run(es.save_snapshot(d, {"AAA": (10.0, 1_000_000)}))
    assert sorted(fake_store) == days[-es.SNAPSHOT_KEEP_SESSIONS:]
    assert es.SNAPSHOT_KEEP_SESSIONS == 5


def test_load_wants_a_completed_session_strictly_before_today(fake_store):
    # only TODAY's snapshot exists (e.g. first evening after deploy)
    asyncio.run(es.save_snapshot("2026-07-06", {"AAA": (10.0, 1_000_000)}))
    assert asyncio.run(es.load_prev_session_map("2026-07-06")) == {}
    # next morning the same snapshot IS the completed prior session
    prev = asyncio.run(es.load_prev_session_map("2026-07-07"))
    assert prev["AAA"] == {"c": 10.0, "v": 1_000_000.0}
    # and the LATEST prior date wins when several exist
    asyncio.run(es.save_snapshot("2026-07-01", {"AAA": (7.0, 5)}))
    prev = asyncio.run(es.load_prev_session_map("2026-07-07"))
    assert prev["AAA"]["c"] == 10.0


def test_load_swallows_db_failure(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(es, "_session_factory", boom)
    assert asyncio.run(es.load_prev_session_map("2026-07-07")) == {}


# ── the capture ─────────────────────────────────────────────────────────────
GAINERS = [{"symbol": "CLRO", "price": 6.48, "change": 3.26,
            "changesPercentage": 101.24, "exchange": "NASDAQ"}]
LOSERS = [{"symbol": "AMPGR", "price": 0.91, "change": -1.22,
           "changesPercentage": -57.28, "exchange": "NASDAQ"}]
ACTIVES = [{"symbol": "CLRO", "price": 6.50, "change": 3.28,
            "changesPercentage": 101.9, "exchange": "NASDAQ"}]
SCREENER = [
    {"symbol": "CLRO", "price": 6.48, "volume": 88_538_476, "marketCap": 11e6},
    {"symbol": "NVDA", "price": 194.83, "volume": 142_385_548, "marketCap": 4.7e12},
]


def _install_capture_mocks(monkeypatch, calls, saved, *, screener=SCREENER):
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    async def fake_get_json(url, params=None, timeout_s=None):
        calls.append(url)
        return {fu.GAINERS_URL: GAINERS, fu.LOSERS_URL: LOSERS,
                fu.ACTIVES_URL: ACTIVES, fu.SCREENER_URL: screener}[url]

    async def fake_save(session_date, quotes):
        saved.append((session_date, dict(quotes)))
        return len(quotes)

    monkeypatch.setattr(fu, "_fmp_get_json", fake_get_json)
    monkeypatch.setattr(es, "save_snapshot", fake_save)


def test_capture_quotes_composition_and_request_budget(monkeypatch):
    calls, saved = [], []
    _install_capture_mocks(monkeypatch, calls, saved)
    n = asyncio.run(es.capture_eod_snapshot("2026-07-02"))
    assert n == 3
    assert len(calls) == 4  # exactly 3 movers + 1 screener
    (date, quotes), = saved
    assert date == "2026-07-02"
    # screener rows carry the completed-session volume
    assert quotes["CLRO"] == (6.48, 88_538_476)   # screener wins over the mover dupe
    assert quotes["NVDA"] == (194.83, 142_385_548)
    # below-sweep mover keeps its close with volume 0 — never fabricated
    assert quotes["AMPGR"] == (0.91, 0.0)


def test_capture_skips_on_empty_key(monkeypatch):
    calls, saved = [], []
    _install_capture_mocks(monkeypatch, calls, saved)
    monkeypatch.setenv("FMP_API_KEY", "")
    assert asyncio.run(es.capture_eod_snapshot("2026-07-02")) == 0
    assert calls == [] and saved == []


def test_capture_survives_all_fetches_failing(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")

    async def boom(url, params=None, timeout_s=None):
        raise RuntimeError("fmp down")

    monkeypatch.setattr(fu, "_fmp_get_json", boom)
    assert asyncio.run(es.capture_eod_snapshot("2026-07-02")) == 0


# ── the scheduler hook: env gate, time gate, SETNX latch ────────────────────
class _FakeRedis:
    def __init__(self, *, raise_on_set=False):
        self.keys: dict = {}
        self.raise_on_set = raise_on_set

    def set(self, key, value, ex=None, nx=None):
        if self.raise_on_set:
            raise ConnectionError("redis down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def delete(self, key):
        self.keys.pop(key, None)


def _wire_hook(monkeypatch, *, et=datetime(2026, 7, 6, 16, 30), redis=None):
    """Monday 16:30 ET by default — inside the capture window."""
    captured = []

    async def fake_capture(session_date=None):
        captured.append(session_date)
        return 1

    monkeypatch.setattr(es, "_now_et", lambda: et)
    monkeypatch.setattr(es, "_get_redis", lambda: redis if redis is not None else _FakeRedis())
    monkeypatch.setattr(es, "capture_eod_snapshot", fake_capture)
    return captured


def test_hook_fires_once_per_day_via_setnx_latch(monkeypatch):
    r = _FakeRedis()
    captured = _wire_hook(monkeypatch, redis=r)

    async def run():
        await es._check_and_run_eod_snapshot()
        await es._check_and_run_eod_snapshot()  # same day → latch blocks

    asyncio.run(run())
    assert captured == ["2026-07-06"]
    assert list(r.keys) == [f"{es.REDIS_LATCH_PREFIX}2026-07-06"]


def test_hook_time_and_weekday_gates(monkeypatch):
    # 16:10 ET — before the 16:15 close-settle gate
    captured = _wire_hook(monkeypatch, et=datetime(2026, 7, 6, 16, 10))
    asyncio.run(es._check_and_run_eod_snapshot())
    assert captured == []
    # Saturday — never
    captured = _wire_hook(monkeypatch, et=datetime(2026, 7, 4, 17, 0))
    asyncio.run(es._check_and_run_eod_snapshot())
    assert captured == []
    # 16:15 sharp — fires
    captured = _wire_hook(monkeypatch, et=datetime(2026, 7, 6, 16, 15))
    asyncio.run(es._check_and_run_eod_snapshot())
    assert captured == ["2026-07-06"]


def test_hook_env_gate_default_on(monkeypatch):
    captured = _wire_hook(monkeypatch)
    monkeypatch.setenv("EOD_SNAPSHOT_ENABLED", "0")
    asyncio.run(es._check_and_run_eod_snapshot())
    assert captured == []
    monkeypatch.delenv("EOD_SNAPSHOT_ENABLED", raising=False)  # default = on
    asyncio.run(es._check_and_run_eod_snapshot())
    assert captured == ["2026-07-06"]


def test_hook_skips_without_redis_latch(monkeypatch):
    """No redis → skip (never risk duplicate daily captures)."""
    captured = _wire_hook(monkeypatch, redis=_FakeRedis(raise_on_set=True))
    asyncio.run(es._check_and_run_eod_snapshot())
    assert captured == []


def test_hook_swallows_capture_failure(monkeypatch):
    monkeypatch.setattr(es, "_now_et", lambda: datetime(2026, 7, 6, 16, 30))
    monkeypatch.setattr(es, "_get_redis", lambda: _FakeRedis())

    async def boom(session_date=None):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(es, "capture_eod_snapshot", boom)
    asyncio.run(es._check_and_run_eod_snapshot())  # must NOT raise


def test_hook_releases_latch_when_capture_persists_nothing(monkeypatch):
    """Self-healing retry: a transient FMP failure (0 rows persisted) must
    NOT burn the whole day's snapshot — the latch is released so a later
    loop iteration retries the same evening; a successful capture keeps it."""
    r = _FakeRedis()
    monkeypatch.setattr(es, "_now_et", lambda: datetime(2026, 7, 6, 16, 30))
    monkeypatch.setattr(es, "_get_redis", lambda: r)
    calls = []

    async def flaky_capture(session_date=None):
        calls.append(session_date)
        return 0 if len(calls) == 1 else 7  # first attempt fails transiently

    monkeypatch.setattr(es, "capture_eod_snapshot", flaky_capture)

    async def run():
        await es._check_and_run_eod_snapshot()  # 0 rows → latch released
        assert list(r.keys) == []
        await es._check_and_run_eod_snapshot()  # retry succeeds → latch kept
        await es._check_and_run_eod_snapshot()  # third same-day → latch blocks

    asyncio.run(run())
    assert calls == ["2026-07-06", "2026-07-06"]
    assert list(r.keys) == [f"{es.REDIS_LATCH_PREFIX}2026-07-06"]


def test_hook_releases_latch_when_capture_raises(monkeypatch):
    """An unexpected exception inside the capture is swallowed AND releases
    the latch (try/finally) so the day still gets a retry."""
    r = _FakeRedis()
    monkeypatch.setattr(es, "_now_et", lambda: datetime(2026, 7, 6, 16, 30))
    monkeypatch.setattr(es, "_get_redis", lambda: r)

    async def boom(session_date=None):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(es, "capture_eod_snapshot", boom)
    asyncio.run(es._check_and_run_eod_snapshot())  # must NOT raise
    assert list(r.keys) == []  # latch released → later iteration can retry


# ── the wiring itself: the hook MUST be called from the scheduler loop ──────
def test_hook_is_wired_into_premarket_scheduler_loop():
    """Regression guard for the exact failure mode that shipped once: the
    module existed, 59 tests were green, and yet _check_and_run_eod_snapshot
    had ZERO call sites (a concurrent-track write clobbered the wiring), so
    the snapshot table stayed empty forever and every morning paid the
    <=200-request bridge. Assert the scheduler source really awaits the hook."""
    import inspect

    import app.engines.options.premarket_scheduler as ps

    src = inspect.getsource(ps)
    assert "from app.engines.data_feeds.fmp_eod_snapshot import" in src
    assert "_check_and_run_eod_snapshot" in src
    assert "await _check_and_run_eod_snapshot()" in src
