"""Unit tests for the public landing-page tape endpoint (NO network, NO DB).

yfinance is FULLY mocked (monkeypatched yf.download); the route runs in an
in-process FastAPI app with NO auth dependency and NO token — asserting the
endpoint really is public.

Covers:
  1. quotes parsed correctly: "=F" tickers map to bare roots (ES=F -> ES),
     prices are comma-grouped 2dp, change_pct is a signed 2dp float,
     NaN/short-history symbols are skipped
  2. 60s TTL: a second call does NOT hit yfinance again (call-count assert)
  3. yfinance blowing up -> HTTP 200 {"live": false, "quotes": []} (never 500)
  4. no auth required (TestClient sends no Authorization header anywhere)
  5. single-flight: N CONCURRENT cold-cache requests share ONE yf.download
     (the TTL test only proves the sequential case)
  6. last-good older than the 15-min cutoff still serves its real quotes but
     flips live -> false (the LIVE pip must never lie about hours-old prices)

Run: pytest tests/test_public_tape.py -q -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import time

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import public_tape as tape_mod


def _make_app() -> FastAPI:
    """In-process app, same prefix as prod main.py. Deliberately NO auth
    dependencies and NO overrides: if the route grew an auth dep these
    tests would 401 and fail — that's the 'must stay public' guard."""
    app = FastAPI()
    app.include_router(tape_mod.router, prefix="/api/v1/public")
    return app


def _fake_df(data: dict[str, tuple[float, float]]) -> pd.DataFrame:
    """Build a frame shaped like yf.download(group_by='ticker') output:
    MultiIndex columns (ticker, field), one row per day."""
    idx = pd.to_datetime(["2026-06-30", "2026-07-01"])
    cols: dict[tuple[str, str], list[float]] = {}
    for sym, (prev_close, last_close) in data.items():
        cols[(sym, "Open")] = [prev_close, last_close]
        cols[(sym, "Close")] = [prev_close, last_close]
        cols[(sym, "Volume")] = [1_000_000.0, 1_100_000.0]
    df = pd.DataFrame(cols, index=idx)
    df.columns = pd.MultiIndex.from_tuples(df.columns)
    return df


@pytest.fixture(autouse=True)
def _reset_tape_state():
    """The route keeps a module-level TTL cache + last-good payload (and its
    fetch timestamp); clear all of it so each test starts cold."""
    tape_mod._cache.clear()
    tape_mod._last_good = None
    tape_mod._last_good_at = 0.0
    yield
    tape_mod._cache.clear()
    tape_mod._last_good = None
    tape_mod._last_good_at = 0.0


def test_quotes_parsed_futures_mapped_and_comma_formatted(monkeypatch):
    calls = {"n": 0}

    def fake_download(*args, **kwargs):
        calls["n"] += 1
        return _fake_df({
            "ES=F": (6000.00, 6204.25),        # +3.40% and comma grouping
            "NQ=F": (30000.00, 30528.75),
            "SPY": (750.00, 748.50),           # -0.20% (signed change)
            "GC=F": (3400.00, float("nan")),   # NaN close -> skipped
        })

    import yfinance
    monkeypatch.setattr(yfinance, "download", fake_download)

    with TestClient(_make_app()) as client:
        r = client.get("/api/v1/public/tape")  # NO Authorization header
    assert r.status_code == 200
    body = r.json()
    assert body["live"] is True
    assert "as_of" in body
    by_sym = {q["symbol"]: q for q in body["quotes"]}

    # "=F" futures map to bare display roots — and the raw yf ticker never leaks.
    assert "ES" in by_sym and "ES=F" not in by_sym
    assert by_sym["ES"]["price"] == "6,204.25"
    assert by_sym["ES"]["change_pct"] == pytest.approx(3.40, abs=0.01)

    # Comma grouping on 5-digit futures.
    assert by_sym["NQ"]["price"] == "30,528.75"

    # Signed (negative) change comes through as a float, 2dp.
    assert by_sym["SPY"]["change_pct"] == pytest.approx(-0.20, abs=0.01)

    # NaN close -> symbol skipped, not emitted as garbage.
    assert "GC" not in by_sym

    assert calls["n"] == 1


def test_second_call_within_ttl_skips_yfinance(monkeypatch):
    calls = {"n": 0}

    def fake_download(*args, **kwargs):
        calls["n"] += 1
        return _fake_df({"ES=F": (6000.00, 6100.00)})

    import yfinance
    monkeypatch.setattr(yfinance, "download", fake_download)

    with TestClient(_make_app()) as client:
        r1 = client.get("/api/v1/public/tape")
        r2 = client.get("/api/v1/public/tape")
    assert r1.status_code == 200 and r2.status_code == 200
    assert calls["n"] == 1, "second call within the 60s TTL must be served from cache"
    assert r1.json() == r2.json()


def test_yfinance_exception_returns_200_dead_tape(monkeypatch):
    def exploding_download(*args, **kwargs):
        raise RuntimeError("yahoo is down")

    import yfinance
    monkeypatch.setattr(yfinance, "download", exploding_download)

    with TestClient(_make_app()) as client:
        r = client.get("/api/v1/public/tape")
    assert r.status_code == 200  # NEVER 500 — decorative endpoint
    body = r.json()
    assert body["live"] is False
    assert body["quotes"] == []


def test_failure_after_success_serves_stale_last_good(monkeypatch):
    """Yahoo dying AFTER a good fetch serves the stale-but-real payload."""
    state = {"n": 0}

    def flaky_download(*args, **kwargs):
        state["n"] += 1
        if state["n"] > 1:
            raise RuntimeError("rate limited")
        return _fake_df({"ES=F": (6000.00, 6100.00)})

    import yfinance
    monkeypatch.setattr(yfinance, "download", flaky_download)

    with TestClient(_make_app()) as client:
        r1 = client.get("/api/v1/public/tape")
        # Kill the TTL cache to force a re-fetch (simulates 60s passing);
        # _last_good survives because only the TTL entry is cleared.
        tape_mod._cache.clear()
        r2 = client.get("/api/v1/public/tape")
    assert state["n"] == 2
    assert r2.status_code == 200
    assert r2.json() == r1.json()  # stale-if-error, still live:true payload


def test_concurrent_cold_cache_requests_share_one_fetch(monkeypatch):
    """Single-flight guard: 8 CONCURRENT requests against a cold cache must
    trigger exactly ONE yf.download. Without the module-level lock every one
    of them would launch its own multi-second download in the shared
    to_thread executor (this is a public, unauthenticated route)."""
    calls = {"n": 0}

    def slow_download(*args, **kwargs):
        calls["n"] += 1
        time.sleep(0.25)  # long enough for all coroutines to pile up on the lock
        return _fake_df({"ES=F": (6000.00, 6100.00)})

    import yfinance
    monkeypatch.setattr(yfinance, "download", slow_download)

    async def hammer():
        # Call the route coroutine directly — TestClient is sync-only and
        # can't express true in-loop concurrency.
        return await asyncio.gather(*(tape_mod.public_tape() for _ in range(8)))

    results = asyncio.run(hammer())
    assert calls["n"] == 1, "concurrent cold-cache requests must share one fetch"
    assert all(r == results[0] for r in results)
    assert results[0]["live"] is True
    assert results[0]["quotes"][0]["symbol"] == "ES"


def test_stale_last_good_past_cutoff_flips_live_false(monkeypatch):
    """last-good older than _STALE_MAX_S still serves its real quotes (better
    than the static fallback) but must stop claiming live:true — the frontend
    drops the LIVE pip on live:false."""
    state = {"n": 0}

    def flaky_download(*args, **kwargs):
        state["n"] += 1
        if state["n"] > 1:
            raise RuntimeError("yahoo has been down for a while")
        return _fake_df({"ES=F": (6000.00, 6100.00)})

    import yfinance
    monkeypatch.setattr(yfinance, "download", flaky_download)

    with TestClient(_make_app()) as client:
        r1 = client.get("/api/v1/public/tape")
        # Simulate >15 min passing: expire the TTL entry and age the
        # last-good timestamp past the cutoff.
        tape_mod._cache.clear()
        tape_mod._last_good_at -= tape_mod._STALE_MAX_S + 1.0
        r2 = client.get("/api/v1/public/tape")

    assert r1.json()["live"] is True
    assert r2.status_code == 200
    body = r2.json()
    assert body["live"] is False, "hours-old quotes must not be badged live"
    assert body["quotes"] == r1.json()["quotes"]  # ...but still served


def test_all_symbols_nan_returns_dead_tape(monkeypatch):
    """yf 'succeeding' with garbage (all NaN) must not fake live:true."""
    def nan_download(*args, **kwargs):
        return _fake_df({"ES=F": (float("nan"), float("nan"))})

    import yfinance
    monkeypatch.setattr(yfinance, "download", nan_download)

    with TestClient(_make_app()) as client:
        r = client.get("/api/v1/public/tape")
    assert r.status_code == 200
    assert r.json()["live"] is False
    assert r.json()["quotes"] == []
