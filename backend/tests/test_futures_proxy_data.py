"""Futures real-time data via Polygon ETF proxy (account_signals.runner).

These verify the DATA-SOURCE change without hitting the network:
  • Polygon returns SPY 1-min bars + a proxy scale of 10  ->  ES bars come back
    scaled (price ~= SPY*10) AND fresh (latest bar age in seconds).
  • Polygon failure (empty/exception)  ->  the futures fast-path returns nothing
    and _fetch_bars_uncached falls through to the existing yfinance path.

Run: pytest backend/tests/test_futures_proxy_data.py -v -p no:cacheprovider
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from app.engines.account_signals import runner as rn


class _FakeResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
    def json(self):
        return self._payload


def _spy_1m_payload(n=10, base_price=740.0):
    """n fresh consecutive 1-min SPY bars ending ~now (last bar age ~ seconds)."""
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    results = []
    for i in range(n):
        ts = now - timedelta(minutes=(n - 1 - i))
        t_ms = int(ts.timestamp() * 1000)
        px = base_price + i * 0.1
        results.append({"t": t_ms, "o": px, "h": px + 0.05, "l": px - 0.05,
                        "c": px, "v": 100000})
    return {"results": results}


# ── 1. Polygon SPY bars + scale 10 -> ES bars scaled & fresh ────────────────
def test_es_via_polygon_is_scaled_and_fresh(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")

    # Pin the proxy scale to exactly 10.0 (deterministic).
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)

    payload = _spy_1m_payload(n=10, base_price=740.0)

    def fake_get(url, timeout=8):
        assert "/ticker/SPY/" in url, f"expected SPY proxy URL, got {url}"
        return _FakeResp(200, payload)

    import requests as _rq
    monkeypatch.setattr(_rq, "get", fake_get, raising=True)

    bars = rn._fetch_futures_via_polygon("ES", "1m", 5)
    assert bars, "expected ES bars from Polygon proxy"
    last = bars[-1]
    # SPY last close 740.0 + 9*0.1 = 740.9; *10 scale => ~7409
    assert 7400 < last["close"] < 7420, f"ES close not scaled: {last['close']}"
    # Fresh: latest bar within ~120s of now.
    age = (datetime.now(timezone.utc) - last["timestamp"]).total_seconds()
    assert age < 120, f"expected fresh bar (<120s), got {age:.0f}s"


# ── 2. Micros (MES) borrow the ES proxy + scale ─────────────────────────────
def test_mes_uses_es_proxy_and_scale(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    import app.engines.data_feeds.proxy_scale as ps
    seen = {}
    def _scale(inst):
        seen["inst"] = inst
        return 10.0
    monkeypatch.setattr(ps, "get_proxy_scale", _scale, raising=True)

    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda url, timeout=8: _FakeResp(200, _spy_1m_payload()), raising=True)

    bars = rn._fetch_futures_via_polygon("MES", "1m", 5)
    assert bars, "MES should resolve via SPY proxy"
    assert seen["inst"] == "ES", f"MES must scale on parent ES root, got {seen['inst']}"


# ── 3. Polygon HTTP error -> empty (caller will fall back) ──────────────────
def test_polygon_http_error_returns_empty(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)
    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda url, timeout=8: _FakeResp(500, {}), raising=True)
    assert rn._fetch_futures_via_polygon("ES", "1m", 5) == []


# ── 4. Polygon exception -> empty (caller will fall back) ───────────────────
def test_polygon_exception_returns_empty(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)
    import requests as _rq
    def boom(url, timeout=8):
        raise RuntimeError("network down")
    monkeypatch.setattr(_rq, "get", boom, raising=True)
    assert rn._fetch_futures_via_polygon("ES", "1m", 5) == []


# ── 4b. Stale proxy bar -> freshness guard returns empty (fall back) ────────
def test_stale_proxy_bar_triggers_fallback(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    # Force a tiny max-age so even ~minutes-old bars are "stale".
    monkeypatch.setenv("FUTURES_PROXY_MAX_AGE_SEC", "30")
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)

    # Build bars whose latest is ~20 minutes old (older than the 30s cap).
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    results = []
    for i in range(5):
        ts = now - timedelta(minutes=(24 - i))  # last bar ~20m old
        results.append({"t": int(ts.timestamp() * 1000), "o": 740.0, "h": 740.1,
                        "l": 739.9, "c": 740.0, "v": 100000})
    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda url, timeout=8: _FakeResp(200, {"results": results}), raising=True)

    assert rn._fetch_futures_via_polygon("ES", "1m", 5) == [], "stale proxy must fall back"


# ── 5. End-to-end: _fetch_bars_uncached(ES) returns the Polygon bars when
#       Polygon succeeds (fast-path taken, candle_cache NOT consulted) ────────
def test_fetch_bars_uncached_prefers_polygon_for_futures(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)
    import requests as _rq
    monkeypatch.setattr(_rq, "get", lambda url, timeout=8: _FakeResp(200, _spy_1m_payload()), raising=True)

    # If the fast-path is taken we should NEVER reach psycopg2.connect.
    import psycopg2
    def _no_db(*a, **k):
        raise AssertionError("candle_cache path should not run when Polygon succeeds")
    monkeypatch.setattr(psycopg2, "connect", _no_db, raising=True)

    bars = rn._fetch_bars_uncached("ES", "1m", 5)
    assert bars, "ES bars expected from the Polygon fast-path"
    assert 7400 < bars[-1]["close"] < 7420


# ── 6. End-to-end: Polygon fails for ES -> falls back to yfinance path ──────
def test_fetch_bars_uncached_falls_back_to_yfinance(monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "test-key")
    import app.engines.data_feeds.proxy_scale as ps
    monkeypatch.setattr(ps, "get_proxy_scale", lambda inst: 10.0, raising=True)
    # Polygon proxy returns nothing.
    monkeypatch.setattr(rn, "_fetch_futures_via_polygon", lambda *a, **k: [], raising=True)
    # candle_cache empty (force the yfinance branch): make psycopg2 return no rows.
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

    # Stub the yfinance-cached fallback so we assert it's the path taken.
    sentinel = [{"timestamp": datetime.now(timezone.utc), "open": 1.0, "high": 1.0,
                 "low": 1.0, "close": 1.0, "volume": 1}]
    called = {"yf": False}
    def _fake_yf(fb_sym, period, timeframe, count):
        called["yf"] = True
        return sentinel
    monkeypatch.setattr(rn, "_yfinance_cached", _fake_yf, raising=True)

    bars = rn._fetch_bars_uncached("ES", "1m", 5)
    assert called["yf"], "yfinance fallback must be used when Polygon + cache miss"
    assert bars == sentinel
