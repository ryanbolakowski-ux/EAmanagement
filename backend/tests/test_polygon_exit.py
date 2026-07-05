"""POLYGON-EXIT — every remaining runtime Polygon dependency is FMP-primary.

Fully mocked (NO live HTTP). Coverage:
  • P&L mark routing: fmp_equity_snapshot_sync builds a Polygon-snapshot-shaped
    dict (quote-short live during RTH, settled EOD close otherwise) and
    pick_equity_mark yields BYTE-IDENTICAL RTH-freeze semantics vs the Polygon
    snapshot path (same value, same source label, after-hours print can never
    move the mark).
  • Bars routing: premarket_scheduler._polygon_1min_bars/_polygon_5min_bars are
    FMP-primary when REALTIME_FEED=fmp (Polygon REST never touched), fall back
    to Polygon when FMP is empty, and stay byte-identical Polygon-only when the
    flag is off (FMP never touched).
  • 1min→5min local resample math (bucket t / o / h / l / c / v).
  • _polygon_last_trade_price routes to FMP quote-short when REALTIME_FEED=fmp.
  • FMP sync helpers: quote-short parsing, settled-close newest-row selection +
    TTL cache (≤1 request/symbol/TTL).
  • systems-check swap: scanner_health probes FMP (not Polygon); the admin
    market_data card is green only with FMP_API_KEY.

Run: pytest backend/tests/test_polygon_exit.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio

import pytest

import app.engines.data_feeds.fmp_feed as fmp
import app.engines.options.premarket_scheduler as pms
import app.engines.scanner_health as sh
from app.engines.pnl_marks import pick_equity_mark


# ── helpers ──────────────────────────────────────────────────────────────────
T0 = 1_751_446_800_000  # epoch ms, exactly 5-min aligned (T0 % 300_000 == 0)
assert T0 % 300_000 == 0


def _bar(t, o, h, l, c, v):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v}


def _boom(*_a, **_k):
    raise AssertionError("this provider must NOT be called in this scenario")


async def _aboom(*_a, **_k):
    raise AssertionError("this provider must NOT be called in this scenario")


class _Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


# ── P&L mark routing + RTH-freeze parity ────────────────────────────────────
def test_fmp_mark_regular_session_uses_quote_short(monkeypatch):
    monkeypatch.setattr(fmp, "fetch_quote_short_price_sync", lambda s, **k: 101.5)
    monkeypatch.setattr(fmp, "fetch_last_settled_close_sync", _boom)  # no EOD in RTH
    tick = fmp.fmp_equity_snapshot_sync("AAPL", "regular")
    px, src = pick_equity_mark(tick, "regular")
    assert px == 101.5
    assert src == "last_trade/regular"


def test_fmp_mark_freezes_outside_rth_never_live_tick(monkeypatch):
    # Outside RTH the FMP snapshot contains ONLY the settled close — a live
    # after-hours print (quote-short) is unreachable BY CONSTRUCTION.
    monkeypatch.setattr(fmp, "fetch_quote_short_price_sync", _boom)
    monkeypatch.setattr(fmp, "fetch_last_settled_close_sync", lambda s, **k: 99.0)
    for sess in ("afterhours", "premarket", "closed"):
        tick = fmp.fmp_equity_snapshot_sync("AAPL", sess)
        assert tick == {"day": {"c": 99.0}}
        px, src = pick_equity_mark(tick, sess)
        assert px == 99.0
        assert src == f"day_close/{sess}"


def test_rth_freeze_parity_fmp_vs_polygon(monkeypatch):
    # Polygon snapshot after hours: lastTrade ticked to 105 but the settled
    # close is 100 — freeze rule marks 100. The FMP path must yield the SAME
    # (price, source) tuple through the SAME decision function.
    poly_tick = {"lastTrade": {"p": 105.0}, "day": {"c": 100.0}, "prevDay": {"c": 98.0}}
    px_p, src_p = pick_equity_mark(poly_tick, "afterhours")

    monkeypatch.setattr(fmp, "fetch_quote_short_price_sync", _boom)
    monkeypatch.setattr(fmp, "fetch_last_settled_close_sync", lambda s, **k: 100.0)
    px_f, src_f = pick_equity_mark(fmp.fmp_equity_snapshot_sync("MSFT", "afterhours"), "afterhours")

    assert (px_p, src_p) == (px_f, src_f) == (100.0, "day_close/afterhours")


def test_fmp_mark_failure_returns_empty_dict(monkeypatch):
    # {} tells the caller to fall through to its existing Polygon snapshot path.
    monkeypatch.setattr(fmp, "fetch_quote_short_price_sync", lambda s, **k: None)
    monkeypatch.setattr(fmp, "fetch_last_settled_close_sync", lambda s, **k: None)
    assert fmp.fmp_equity_snapshot_sync("AAPL", "regular") == {}
    assert fmp.fmp_equity_snapshot_sync("AAPL", "afterhours") == {}


# ── FMP sync helper parsing ──────────────────────────────────────────────────
def test_quote_short_sync_parses_price(monkeypatch):
    import requests
    seen = {}

    def fake_get(url, params=None, timeout=None, **kw):
        seen["url"] = url
        seen["params"] = params or {}
        return _Resp(200, [{"symbol": "AAPL", "price": 187.42, "volume": 123}])

    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr(requests, "get", fake_get)
    assert fmp.fetch_quote_short_price_sync("aapl") == 187.42
    assert "quote-short" in seen["url"]
    assert seen["params"].get("symbol") == "AAPL"
    assert "polygon" not in seen["url"]


def test_quote_short_sync_never_raises(monkeypatch):
    import requests
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr(requests, "get", _boom)
    assert fmp.fetch_quote_short_price_sync("AAPL") is None
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    assert fmp.fetch_quote_short_price_sync("AAPL") is None  # no key -> no HTTP


def test_settled_close_picks_newest_row_and_caches(monkeypatch):
    import requests
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None, **kw):
        calls["n"] += 1
        return _Resp(200, [
            {"date": "2026-07-02", "close": 99.0},
            {"date": "2026-07-03", "close": 101.0},   # newest settled session
            {"date": "2026-07-01", "close": 97.0},
        ])

    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr(requests, "get", fake_get)
    # unique symbol so the module-level TTL cache can't leak across tests
    assert fmp.fetch_last_settled_close_sync("PXEXIT1") == 101.0
    assert fmp.fetch_last_settled_close_sync("PXEXIT1") == 101.0  # served from cache
    assert calls["n"] == 1


# ── 1min→5min resample math ──────────────────────────────────────────────────
def test_resample_1min_to_5min_math():
    one = 60_000
    bars = [
        _bar(T0 + 0 * one, 10.0, 11.0, 9.5, 10.5, 100),
        _bar(T0 + 1 * one, 10.5, 12.0, 10.0, 11.0, 50),
        _bar(T0 + 2 * one, 11.0, 11.5, 10.8, 11.2, 25),
        _bar(T0 + 5 * one, 20.0, 21.0, 19.0, 20.5, 10),  # next 5-min bucket
    ]
    out = pms._resample_1min_to_5min(bars)
    assert out == [
        {"t": T0, "o": 10.0, "h": 12.0, "l": 9.5, "c": 11.2, "v": 175.0},
        {"t": T0 + 300_000, "o": 20.0, "h": 21.0, "l": 19.0, "c": 20.5, "v": 10.0},
    ]


def test_resample_drops_garbage_and_handles_empty():
    assert pms._resample_1min_to_5min([]) == []
    assert pms._resample_1min_to_5min(None) == []
    out = pms._resample_1min_to_5min([
        {"t": 0, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},      # unusable ts
        {"t": "junk", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1},  # unparsable
        _bar(T0, 5.0, 6.0, 4.0, 5.5, 7),
    ])
    assert out == [{"t": T0, "o": 5.0, "h": 6.0, "l": 4.0, "c": 5.5, "v": 7.0}]


# ── bars routing (premarket_scheduler helpers) ───────────────────────────────
def test_1min_bars_fmp_primary_polygon_untouched(monkeypatch):
    monkeypatch.setenv("REALTIME_FEED", "fmp")
    fake = [_bar(T0, 1.0, 2.0, 0.5, 1.5, 100)]

    async def fake_fetch(symbol, n=None, date_et=None, **kw):
        assert symbol == "AAPL" and date_et == "2026-07-02"
        return fake

    monkeypatch.setattr(fmp, "fetch_intraday_bars", fake_fetch)
    monkeypatch.setattr(pms, "_poly_get", _aboom)  # Polygon must NOT be hit
    assert asyncio.run(pms._polygon_1min_bars("AAPL", "2026-07-02")) == fake


def test_1min_bars_fall_back_to_polygon_when_fmp_empty(monkeypatch):
    monkeypatch.setenv("REALTIME_FEED", "fmp")
    monkeypatch.setenv("POLYGON_API_KEY", "poly-key")

    async def empty_fetch(*a, **k):
        return []

    poly = [_bar(T0, 3.0, 4.0, 2.0, 3.5, 42)]

    async def fake_poly_get(url, params, timeout=4.0):
        assert "api.polygon.io" in url
        return _Resp(200, {"results": poly})

    monkeypatch.setattr(fmp, "fetch_intraday_bars", empty_fetch)
    monkeypatch.setattr(pms, "_poly_get", fake_poly_get)
    assert asyncio.run(pms._polygon_1min_bars("AAPL", "2026-07-02")) == poly


def test_1min_bars_flag_off_is_polygon_only(monkeypatch):
    monkeypatch.delenv("REALTIME_FEED", raising=False)
    monkeypatch.setenv("POLYGON_API_KEY", "poly-key")
    monkeypatch.setattr(fmp, "fetch_intraday_bars", _aboom)  # FMP must NOT be hit
    poly = [_bar(T0, 3.0, 4.0, 2.0, 3.5, 42)]

    async def fake_poly_get(url, params, timeout=4.0):
        return _Resp(200, {"results": poly})

    monkeypatch.setattr(pms, "_poly_get", fake_poly_get)
    assert asyncio.run(pms._polygon_1min_bars("AAPL", "2026-07-02")) == poly


def test_5min_bars_fmp_primary_resamples_locally(monkeypatch):
    monkeypatch.setenv("REALTIME_FEED", "fmp")
    one = 60_000
    fake_1m = [
        _bar(T0 + 0 * one, 10.0, 11.0, 9.5, 10.5, 100),
        _bar(T0 + 1 * one, 10.5, 12.0, 10.0, 11.0, 50),
        _bar(T0 + 5 * one, 20.0, 21.0, 19.0, 20.5, 10),
    ]

    async def fake_fetch(symbol, n=None, date_et=None, **kw):
        return fake_1m

    monkeypatch.setattr(fmp, "fetch_intraday_bars", fake_fetch)
    monkeypatch.setattr(pms, "_poly_get", _aboom)
    out = asyncio.run(pms._polygon_5min_bars("NVDA", "2026-07-02"))
    assert out == [
        {"t": T0, "o": 10.0, "h": 12.0, "l": 9.5, "c": 11.0, "v": 150.0},
        {"t": T0 + 300_000, "o": 20.0, "h": 21.0, "l": 19.0, "c": 20.5, "v": 10.0},
    ]


def test_5min_bars_flag_off_is_polygon_only(monkeypatch):
    monkeypatch.delenv("REALTIME_FEED", raising=False)
    monkeypatch.setenv("POLYGON_API_KEY", "poly-key")
    monkeypatch.setattr(fmp, "fetch_intraday_bars", _aboom)
    poly = [_bar(T0, 1.0, 1.0, 1.0, 1.0, 1)]

    async def fake_poly_get(url, params, timeout=4.0):
        assert "range/5/minute" in url
        return _Resp(200, {"results": poly})

    monkeypatch.setattr(pms, "_poly_get", fake_poly_get)
    assert asyncio.run(pms._polygon_5min_bars("AAPL", "2026-07-02")) == poly


def test_last_trade_price_fmp_primary(monkeypatch):
    monkeypatch.setenv("REALTIME_FEED", "fmp")

    async def fake_quote(symbol, **kw):
        return 42.5

    monkeypatch.setattr(fmp, "fetch_quote_short_price", fake_quote)
    monkeypatch.setattr(pms, "_poly_get", _aboom)
    assert asyncio.run(pms._polygon_last_trade_price("AAPL")) == 42.5


def test_last_trade_price_flag_off_uses_polygon_snapshot(monkeypatch):
    monkeypatch.delenv("REALTIME_FEED", raising=False)
    monkeypatch.setenv("POLYGON_API_KEY", "poly-key")
    monkeypatch.setattr(fmp, "fetch_quote_short_price", _aboom)

    async def fake_poly_get(url, params, timeout=4.0):
        assert "snapshot" in url
        return _Resp(200, {"ticker": {"lastTrade": {"p": 55.5}}})

    monkeypatch.setattr(pms, "_poly_get", fake_poly_get)
    assert asyncio.run(pms._polygon_last_trade_price("AAPL")) == 55.5


# ── systems-check swap ───────────────────────────────────────────────────────
def test_scanner_health_probes_fmp_not_polygon(monkeypatch):
    import httpx
    seen = {}

    def fake_get(url, params=None, timeout=None, **kw):
        seen["url"] = url
        seen["params"] = params or {}
        return _Resp(200, [{"symbol": "SPY", "price": 512.3}])

    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "get", fake_get)
    comp = sh.probe_market_data()
    assert comp == {"ok": True, "status": 200}
    assert "financialmodelingprep.com" in seen["url"]
    assert "polygon" not in seen["url"]
    assert seen["params"].get("symbol") == "SPY"


def test_scanner_health_probe_flags_missing_fmp_key(monkeypatch):
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        sh.probe_market_data()


def test_scanner_health_probe_empty_body_is_not_ok(monkeypatch):
    import httpx
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, []))
    assert sh.probe_market_data()["ok"] is False


def test_admin_market_data_component_requires_fmp_key(monkeypatch):
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    comp = sh.market_data_component()
    assert comp["status"] == "green"
    assert comp["providers"][0] == "fmp"

    monkeypatch.delenv("FMP_API_KEY", raising=False)
    monkeypatch.setenv("POLYGON_API_KEY", "poly-key")  # legacy key alone ≠ green
    assert sh.market_data_component()["status"] == "yellow"


# ── [fmp-primary] once-per-site log dedupe ───────────────────────────────────
def test_log_fmp_primary_once_dedupes(monkeypatch):
    lines = []
    monkeypatch.setattr(fmp, "logger", type("L", (), {"info": staticmethod(lambda m: lines.append(m)),
                                                      "warning": staticmethod(lambda m: None)})())
    site = "test.site.dedupe-xyz"
    fmp._fmp_primary_logged.discard(site)
    fmp.log_fmp_primary_once(site)
    fmp.log_fmp_primary_once(site)
    assert len([l for l in lines if site in l]) == 1
    assert "[fmp-primary]" in lines[0]
