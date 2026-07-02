"""Scanner V2 fire gates + shadow-runner idempotency.

Gate matrix (docs/v2/01-scanner-forensics.md §5):
  * 06:00 thin microcap        -> NO  (the CAST failure mode)
  * 06:00 liquid + catalyst    -> YES (the measured 39%-WR premarket bucket)
  * 09:40 confirmed            -> YES
  * 10:05 anything             -> NO  (hard close; late fires 0-for-4 -4.05 avg)

Shadow runner: exactly-once per ET day via the theta:shadow_v2:{date} Redis
latch, env-flag kill switch, fail-closed when Redis is unavailable — all with
mocked redis/db (same patterns as test_theta_scanner_no_pick_alert.py).

Run: pytest backend/tests/test_scanner_v2_gates.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from app.engines.scanner.v2.gates import (
    FireDecision, decide_fire,
    EARLIEST_FIRE_MIN, RTH_FIRE_OPEN_MIN, HARD_CLOSE_MIN, PREMARKET_MIN_DOLLAR_VOL,
)

T_0600 = 6 * 60
T_0940 = 9 * 60 + 40


def _cand(pm_dv=None, catalyst=None, catalyst_weight=None, confirmed=False):
    c = {"ticker": "TEST", "price": 6.0, "gap_pct": 8.0, "rel_vol": 12.0,
         "confirmed": confirmed}
    if pm_dv is not None:
        c["premarket_dollar_vol"] = pm_dv
    if catalyst is not None:
        c["catalyst_reason"] = catalyst
    if catalyst_weight is not None:
        c["catalyst_weight"] = catalyst_weight
    return c


# ── premarket window: hard $1M floor AND catalyst ───────────────────────────

def test_0600_thin_microcap_no_fire():
    d = decide_fire(T_0600, _cand(pm_dv=200_000, catalyst="8-K: contract win"))
    assert isinstance(d, FireDecision)
    assert d.allowed is False
    assert d.window == "premarket"


def test_0600_liquid_with_catalyst_fires():
    d = decide_fire(T_0600, _cand(pm_dv=2_000_000, catalyst="8-K: FDA approval"))
    assert d.allowed is True
    assert d.window == "premarket"


def test_0600_liquid_without_catalyst_no_fire():
    d = decide_fire(T_0600, _cand(pm_dv=5_000_000))
    assert d.allowed is False
    assert "catalyst" in d.reason


def test_0600_catalyst_weight_counts_as_catalyst():
    d = decide_fire(T_0600, _cand(pm_dv=2_000_000, catalyst_weight=1.4))
    assert d.allowed is True


def test_0600_pseudo_catalyst_high_relvol_gap_rejected():
    # the V1 funnel labels bare volume surges "high rel-vol gap" — not news
    d = decide_fire(T_0600, _cand(pm_dv=2_000_000, catalyst="high rel-vol gap"))
    assert d.allowed is False


def test_premarket_missing_liquidity_fails_closed():
    # no premarket_dollar_vol measurement -> cannot clear the floor
    d = decide_fire(T_0600, _cand(catalyst="8-K: earnings"))
    assert d.allowed is False


def test_exactly_at_1m_floor_fires():
    d = decide_fire(T_0600, _cand(pm_dv=PREMARKET_MIN_DOLLAR_VOL, catalyst="8-K: merger"))
    assert d.allowed is True


def test_before_0600_no_fire():
    d = decide_fire(EARLIEST_FIRE_MIN - 30, _cand(pm_dv=5_000_000, catalyst="8-K: merger"))
    assert d.allowed is False


# ── RTH window 09:35-10:00: confirmation required ───────────────────────────

def test_0940_confirmed_fires():
    d = decide_fire(T_0940, _cand(confirmed=True))
    assert d.allowed is True
    assert d.window == "rth"


def test_0940_unconfirmed_no_fire():
    d = decide_fire(T_0940, _cand(confirmed=False))
    assert d.allowed is False
    assert "confirmation" in d.reason


def test_opening_rotation_0932_blocked_even_confirmed():
    d = decide_fire(9 * 60 + 32, _cand(confirmed=True))
    assert d.allowed is False


def test_0935_boundary_opens_rth_window():
    d = decide_fire(RTH_FIRE_OPEN_MIN, _cand(confirmed=True))
    assert d.allowed is True


# ── HARD CLOSE at 10:00 — no last-chance tier ───────────────────────────────

def test_1005_no_fire_even_perfect_candidate():
    d = decide_fire(10 * 60 + 5,
                    _cand(pm_dv=50_000_000, catalyst="8-K: buyout", confirmed=True))
    assert d.allowed is False
    assert d.window == "closed"


def test_1000_exactly_is_closed():
    d = decide_fire(HARD_CLOSE_MIN, _cand(confirmed=True))
    assert d.allowed is False


def test_afternoon_closed():
    assert decide_fire(13 * 60, _cand(confirmed=True)).allowed is False


# ── shadow runner idempotency (mocked redis + db) ───────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _LatchRedis:
    """SETNX-faithful fake shared across from_url() calls via `store`."""
    def __init__(self, store: set):
        self._store = store

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self._store:
            return None  # redis-py returns None when NX blocks the set
        self._store.add(key)
        return True


def _patch_runner_env(monkeypatch, calls: list, latch_store: set):
    from app.engines.scanner.v2 import shadow as sv2
    import app.database as appdb
    import redis

    async def _fake_run(db, **kw):
        calls.append(db)
        return {"persisted": 0}

    monkeypatch.setattr(sv2, "run_v2_shadow_scan", _fake_run)
    monkeypatch.setattr(appdb, "async_session_factory", lambda: _FakeSession())
    monkeypatch.setattr(redis.Redis, "from_url",
                        classmethod(lambda cls, *a, **k: _LatchRedis(latch_store)))
    return sv2


def test_shadow_v2_runs_exactly_once_per_day(monkeypatch):
    calls: list = []
    latch: set = set()
    sv2 = _patch_runner_env(monkeypatch, calls, latch)
    now = datetime(2026, 6, 30, 10, 0)  # Tuesday 10:00 ET — inside the window

    _run(sv2._check_and_run_v2_shadow_scan(_now_et=now))
    _run(sv2._check_and_run_v2_shadow_scan(_now_et=now))
    _run(sv2._check_and_run_v2_shadow_scan(_now_et=now))

    assert len(calls) == 1
    assert "theta:shadow_v2:2026-06-30" in latch


def test_shadow_v2_new_day_new_run(monkeypatch):
    calls: list = []
    latch: set = set()
    sv2 = _patch_runner_env(monkeypatch, calls, latch)

    _run(sv2._check_and_run_v2_shadow_scan(_now_et=datetime(2026, 6, 30, 10, 0)))
    _run(sv2._check_and_run_v2_shadow_scan(_now_et=datetime(2026, 7, 1, 10, 0)))

    assert len(calls) == 2
    assert {"theta:shadow_v2:2026-06-30", "theta:shadow_v2:2026-07-01"} <= latch


def test_shadow_v2_env_flag_disables(monkeypatch):
    calls: list = []
    latch: set = set()
    sv2 = _patch_runner_env(monkeypatch, calls, latch)
    monkeypatch.setenv("SCANNER_V2_SHADOW_ENABLED", "0")

    _run(sv2._check_and_run_v2_shadow_scan(_now_et=datetime(2026, 6, 30, 10, 0)))

    assert calls == []
    assert latch == set()  # disabled runs must not even consume the latch


def test_shadow_v2_skips_before_window_and_weekend(monkeypatch):
    calls: list = []
    latch: set = set()
    sv2 = _patch_runner_env(monkeypatch, calls, latch)

    _run(sv2._check_and_run_v2_shadow_scan(_now_et=datetime(2026, 6, 30, 9, 30)))  # pre-window
    _run(sv2._check_and_run_v2_shadow_scan(_now_et=datetime(2026, 6, 28, 10, 0)))  # Sunday

    assert calls == []


def test_shadow_v2_redis_down_fails_closed(monkeypatch):
    calls: list = []
    latch: set = set()
    sv2 = _patch_runner_env(monkeypatch, calls, latch)
    import redis

    def _boom(cls, *a, **k):
        raise ConnectionError("redis down")

    monkeypatch.setattr(redis.Redis, "from_url", classmethod(_boom))
    _run(sv2._check_and_run_v2_shadow_scan(_now_et=datetime(2026, 6, 30, 10, 0)))

    assert calls == []  # no latch -> skip rather than risk duplicate runs
