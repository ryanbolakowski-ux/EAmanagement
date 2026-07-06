"""Alpaca IEX real-time feed adapter + its wiring as the PREFERRED futures source.

No network is hit — the Alpaca HTTP call (httpx.get) and the proxy-scale lookup
are monkeypatched. Coverage:

  • fetch_alpaca_bars: mocked bars JSON -> DataFrame with open/high/low/close/
    volume columns and a tz-aware UTC DatetimeIndex.
  • Missing ALPACA_API_KEY / ALPACA_API_SECRET -> returns None (no crash).
  • Non-200 / malformed responses -> None.
  • runner: with keys set + mocked fresh SPY bars, _fetch_bars_uncached("ES")
    returns Alpaca-sourced bars scaled to the futures level (price ~= SPY*scale),
    and the slower Polygon/candle_cache paths are NOT consulted.
  • Alpaca miss -> falls through to Polygon, then to the yfinance fallback.
  • OVERNIGHT-RELEVANCE-V1: stale Alpaca proxy bars (IEX quiet overnight) are
    discarded past FUTURES_ALPACA_MAX_AGE_SEC (default 900s) so the cascade
    reaches real futures sources; the ceiling is env-tunable.
  • PRICE-BASIS-V1: _note_bar_source/get_price_basis round-trip, staleness
    window, and the basis note recorded when the Alpaca path serves.

Run: pytest backend/tests/test_alpaca_feed.py -v -p no:cacheprovider
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pandas as pd
import pytest

from app.engines.data_feeds import alpaca_feed as af
from app.engines.account_signals import runner as rn


# ── Fakes ───────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


def _alpaca_bars_payload(n=10, base_price=740.0):
    """n fresh consecutive 1-min IEX bars ending ~now (Alpaca RFC-3339 `t`)."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    bars = []
    for i in range(n):
        ts = now - timedelta(minutes=(n - 1 - i))
        px = base_price + i * 0.1
        bars.append({
            "t": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "o": px, "h": px + 0.05, "l": px - 0.05, "c": px,
            "v": 100000, "n": 500, "vw": px,
        })
    return {"bars": bars, "symbol": "SPY", "next_page_token": None}


def _set_keys(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")


def _patch_httpx(monkeypatch, resp, capture=None):
    import httpx

    def fake_get(url, headers=None, params=None, timeout=None):
        if capture is not None:
            capture["url"] = url
            capture["headers"] = headers or {}
            capture["params"] = params or {}
        return resp

    monkeypatch.setattr(httpx, "get", fake_get, raising=True)


# ── 1. Happy path: mocked bars -> DataFrame, right columns + tz-aware index ──
def test_fetch_alpaca_bars_returns_dataframe(monkeypatch):
    _set_keys(monkeypatch)
    cap = {}
    _patch_httpx(monkeypatch, _FakeResp(200, _alpaca_bars_payload(n=10)), capture=cap)

    df = af.fetch_alpaca_bars("SPY", timeframe="1Min", limit=50)

    assert df is not None, "expected a DataFrame"
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 10
    # tz-aware UTC DatetimeIndex.
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.tz is not None, "index must be tz-aware"
    assert str(df.index.tz) in ("UTC", "datetime.timezone.utc")
    # Sorted ascending; last close matches the last synthetic bar.
    assert df.index.is_monotonic_increasing
    assert abs(float(df["close"].iloc[-1]) - (740.0 + 9 * 0.1)) < 1e-6
    # Numeric dtypes.
    for col in ("open", "high", "low", "close", "volume"):
        assert pd.api.types.is_float_dtype(df[col])
    # The free IEX feed + auth headers were actually requested.
    assert cap["params"].get("feed") == "iex"
    assert cap["headers"].get("APCA-API-KEY-ID") == "test-key-id"
    assert cap["headers"].get("APCA-API-SECRET-KEY") == "test-secret"
    assert "/stocks/SPY/bars" in cap["url"]


# ── 2. Missing keys -> None (no crash), and httpx must NOT be called ────────
def test_missing_keys_returns_none(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)

    import httpx
    def _boom(*a, **k):
        raise AssertionError("httpx.get must not be called when keys are unset")
    monkeypatch.setattr(httpx, "get", _boom, raising=True)

    assert af.fetch_alpaca_bars("SPY") is None


def test_only_one_key_set_returns_none(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "only-id")
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    assert af.fetch_alpaca_bars("SPY") is None


# ── 3. Non-200 / empty / exception -> None ──────────────────────────────────
def test_http_error_returns_none(monkeypatch):
    _set_keys(monkeypatch)
    _patch_httpx(monkeypatch, _FakeResp(403, {"message": "forbidden"}))
    assert af.fetch_alpaca_bars("SPY") is None


def test_empty_bars_returns_none(monkeypatch):
    _set_keys(monkeypatch)
    _patch_httpx(monkeypatch, _FakeResp(200, {"bars": []}))
    assert af.fetch_alpaca_bars("SPY") is None


def test_network_exception_returns_none(monkeypatch):
    _set_keys(monkeypatch)
    import httpx
    def boom(url, headers=None, params=None, timeout=None):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(httpx, "get", boom, raising=True)
    assert af.fetch_alpaca_bars("SPY") is None


# ── 4. Runner prefers Alpaca: ES bars scaled (price ~= SPY*scale) ───────────
def test_runner_prefers_alpaca_for_es(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")

    # Pin proxy scale to exactly 10.0.
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)

    # Alpaca returns fresh SPY bars (last close 740.9).
    fresh = _build_spy_df(n=10, base_price=740.0)
    monkeypatch.setattr(af, "fetch_alpaca_bars",
                        lambda symbol, timeframe="1Min", limit=200: fresh.copy(),
                        raising=True)

    # The slower paths must NOT run when Alpaca wins.
    def _no_polygon(*a, **k):
        raise AssertionError("Polygon path must not run when Alpaca succeeds")
    monkeypatch.setattr(rn, "_fetch_futures_via_polygon", _no_polygon, raising=True)
    import psycopg2
    def _no_db(*a, **k):
        raise AssertionError("candle_cache must not run when Alpaca succeeds")
    monkeypatch.setattr(psycopg2, "connect", _no_db, raising=True)

    bars = rn._fetch_bars_uncached("ES", "1m", 5)
    assert bars, "expected Alpaca-sourced ES bars"
    last = bars[-1]
    # 740.9 * 10 ≈ 7409.
    assert 7400 < last["close"] < 7420, f"ES close not scaled: {last['close']}"
    # Fresh (real-time): latest bar within ~120s.
    age = (datetime.now(timezone.utc) - last["timestamp"]).total_seconds()
    assert age < 120, f"expected fresh Alpaca bar (<120s), got {age:.0f}s"


def _build_spy_df(n=10, base_price=740.0):
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    idx = pd.to_datetime([now - timedelta(minutes=(n - 1 - i)) for i in range(n)], utc=True)
    data = {
        "open": [base_price + i * 0.1 for i in range(n)],
        "high": [base_price + i * 0.1 + 0.05 for i in range(n)],
        "low": [base_price + i * 0.1 - 0.05 for i in range(n)],
        "close": [base_price + i * 0.1 for i in range(n)],
        "volume": [100000.0 for _ in range(n)],
    }
    return pd.DataFrame(data, index=idx)


# ── 4b. OVERNIGHT-RELEVANCE-V1: stale Alpaca proxy bars are discarded ────────
def _old_spy_df(minutes_old=20, n=5, base_price=740.0):
    """SPY bars whose LATEST bar is `minutes_old` minutes in the past."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    idx = pd.to_datetime(
        [now - timedelta(minutes=minutes_old + (n - 1 - i)) for i in range(n)], utc=True)
    return pd.DataFrame({
        "open": [base_price] * n, "high": [base_price + 0.1] * n,
        "low": [base_price - 0.1] * n, "close": [base_price] * n,
        "volume": [100000.0] * n,
    }, index=idx)


def test_alpaca_stale_bars_discarded_overnight(monkeypatch):
    """IEX goes quiet overnight/weekends while futures keep trading on Globex:
    a latest bar older than FUTURES_ALPACA_MAX_AGE_SEC (default 900s) must be
    DISCARDED (return []) so the cascade continues to real futures sources —
    it used to short-circuit on hours-old proxy prices."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.delenv("FUTURES_ALPACA_MAX_AGE_SEC", raising=False)
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)

    old = _old_spy_df(minutes_old=20)  # 20 min > the 900s default
    monkeypatch.setattr(af, "fetch_alpaca_bars",
                        lambda symbol, timeframe="1Min", limit=200: old.copy(),
                        raising=True)

    assert rn._fetch_futures_via_alpaca("ES", "1m", 5) == []


def test_alpaca_stale_guard_is_env_tunable_and_ignores_polygon_knob(monkeypatch):
    """FUTURES_ALPACA_MAX_AGE_SEC raises the ceiling; the Polygon-path knob
    (FUTURES_PROXY_MAX_AGE_SEC) still has no effect on the Alpaca path."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    monkeypatch.setenv("FUTURES_ALPACA_MAX_AGE_SEC", "3600")
    monkeypatch.setenv("FUTURES_PROXY_MAX_AGE_SEC", "30")  # Polygon-only knob
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)

    old = _old_spy_df(minutes_old=20)  # 20 min < the 3600s override
    monkeypatch.setattr(af, "fetch_alpaca_bars",
                        lambda symbol, timeframe="1Min", limit=200: old.copy(),
                        raising=True)

    bars = rn._fetch_futures_via_alpaca("ES", "1m", 5)
    assert bars, "bars within the tuned ceiling must be returned"
    assert 7390 < bars[-1]["close"] < 7410


# ── 4c. PRICE-BASIS-V1: bar-source note + staleness window ──────────────────
def test_price_basis_recorded_and_expires():
    rn._note_bar_source("ES", "live SPY IEX proxy scaled to ES (Alpaca real-time)")
    basis = rn.get_price_basis("es")  # case-insensitive
    assert basis and "SPY" in basis
    # A stale note is worse than none: outside max_age_sec -> None.
    assert rn.get_price_basis("ES", max_age_sec=-1.0) is None
    assert rn.get_price_basis("NEVER-FETCHED") is None


def test_price_basis_noted_when_alpaca_serves(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)
    fresh = _build_spy_df(n=10, base_price=740.0)
    monkeypatch.setattr(af, "fetch_alpaca_bars",
                        lambda symbol, timeframe="1Min", limit=200: fresh.copy(),
                        raising=True)
    rn._BAR_SOURCE.pop("ES", None)

    bars = rn._fetch_bars_uncached("ES", "1m", 5)
    assert bars
    basis = rn.get_price_basis("ES")
    assert basis and "IEX" in basis and "Alpaca" in basis


# ── 5. Alpaca miss -> Polygon fallback runs ─────────────────────────────────
def test_alpaca_miss_falls_back_to_polygon(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")

    # Alpaca returns nothing.
    monkeypatch.setattr(af, "fetch_alpaca_bars",
                        lambda symbol, timeframe="1Min", limit=200: None,
                        raising=True)

    # Polygon proxy returns a sentinel set of bars.
    sentinel = [{"timestamp": datetime.now(timezone.utc), "open": 1.0, "high": 1.0,
                 "low": 1.0, "close": 5000.0, "volume": 1}]
    called = {"polygon": False}
    def _poly(instrument, timeframe, count=50):
        called["polygon"] = True
        return list(sentinel)
    monkeypatch.setattr(rn, "_fetch_futures_via_polygon", _poly, raising=True)

    bars = rn._fetch_bars_uncached("ES", "1m", 5)
    assert called["polygon"], "Polygon must be consulted when Alpaca misses"
    assert bars and bars[-1]["close"] == 5000.0


# ── 6. Alpaca + Polygon both miss -> yfinance fallback runs ─────────────────
def test_alpaca_and_polygon_miss_falls_back_to_yfinance(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key-id")
    monkeypatch.setenv("ALPACA_API_SECRET", "test-secret")

    monkeypatch.setattr(af, "fetch_alpaca_bars",
                        lambda symbol, timeframe="1Min", limit=200: None, raising=True)
    monkeypatch.setattr(rn, "_fetch_futures_via_polygon",
                        lambda *a, **k: [], raising=True)

    # candle_cache empty -> force the yfinance branch.
    import psycopg2

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a, **k): pass
        def fetchall(self): return []
    class _Conn:
        def cursor(self): return _Cur()
        def close(self): pass
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: _Conn(), raising=True)

    sentinel = [{"timestamp": datetime.now(timezone.utc), "open": 1.0, "high": 1.0,
                 "low": 1.0, "close": 1.0, "volume": 1}]
    called = {"yf": False}
    def _fake_yf(fb_sym, period, timeframe, count):
        called["yf"] = True
        return sentinel
    monkeypatch.setattr(rn, "_yfinance_cached", _fake_yf, raising=True)

    bars = rn._fetch_bars_uncached("ES", "1m", 5)
    assert called["yf"], "yfinance fallback must run when Alpaca + Polygon + cache all miss"
    assert bars == sentinel


# ── 7. No Alpaca keys -> Alpaca helper short-circuits to [] (Polygon used) ──
def test_no_keys_alpaca_helper_returns_empty(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET", raising=False)
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)
    # Even if fetch_alpaca_bars would return data, the helper must bail before
    # calling it when ALPACA_API_KEY is unset.
    def _should_not_call(*a, **k):
        raise AssertionError("fetch_alpaca_bars must not be called without keys")
    monkeypatch.setattr(af, "fetch_alpaca_bars", _should_not_call, raising=True)
    assert rn._fetch_futures_via_alpaca("ES", "1m", 5) == []
