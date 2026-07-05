"""TRACK fmp-universe — FMP-sourced candidate universe + parallel-validation hook.

All HTTP is mocked (no live FMP/Polygon calls). Coverage:
  • Shape parity: an FMP-built row and a hand-built Polygon-grouped row carry
    the same keys and produce IDENTICAL funnel._coarse candidates.
  • prevClose derivation (price - change, gainers AND losers) +
    changesPercentage passthrough.
  • Dedupe by symbol across gainers/losers/actives/screener (movers win,
    day.v joined from the screener sweep; unknown volume stays 0).
  • Screener-only rows take prevDay {c,v} from the Polygon completed-session
    map; symbols without a prev-map entry are skipped.
  • 60s TTL cache: one build per TTL window.
  • Flag routing: SARO_UNIVERSE=fmp routes _fetch_market_snapshot to the FMP
    universe; default (unset/polygon) never touches FMP; the snapshot cache is
    source-tagged so an env flip can't serve the other source's rows.
  • Fallback: FMP raise OR too-thin build → Polygon path, unchanged.
  • [universe-compare] hook: default OFF, flag-gated, throttled, logs one
    structured line on success, logs-without-raising when FMP fails.

Run: pytest backend/tests/test_fmp_universe.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import json

import pytest
from loguru import logger

import app.engines.data_feeds.fmp_universe as fu
import app.engines.options.momentum_scanner as ms
from app.engines.scanner.definitions import TEMPLATES
from app.engines.scanner.funnel import _coarse


# ── fakes / helpers ─────────────────────────────────────────────────────────
GAINERS = [
    # real probed shapes (2026-07-05): NO volume field in movers
    {"symbol": "CLRO", "price": 6.48, "name": "ClearOne, Inc.", "change": 3.26,
     "changesPercentage": 101.24224, "exchange": "NASDAQ"},
    {"symbol": "TSTA", "price": 10.0, "name": "Test A", "change": 1.0,
     "changesPercentage": 11.1111, "exchange": "NASDAQ"},
]
LOSERS = [
    {"symbol": "AMPGR", "price": 0.91, "name": "Amplitech Right", "change": -1.22,
     "changesPercentage": -57.277, "exchange": "NASDAQ"},
]
ACTIVES = [
    {"symbol": "TSTA", "price": 10.02, "name": "Test A dupe", "change": 1.02,
     "changesPercentage": 11.3333, "exchange": "NASDAQ"},
    {"symbol": "SOXS", "price": 4.51, "name": "3x Bear", "change": 0.65,
     "changesPercentage": 16.83938, "exchange": "AMEX"},
]
SCREENER = [
    {"symbol": "TSTA", "price": 10.0, "volume": 5_000_000, "marketCap": 5e8, "isEtf": False},
    {"symbol": "CLRO", "price": 6.48, "volume": 88_538_476, "marketCap": 11_284_136, "isEtf": False},
    {"symbol": "SCRA", "price": 50.0, "volume": 2_000_000, "marketCap": 9e9, "isEtf": False},
    {"symbol": "SCRB", "price": 20.0, "volume": 900_000, "marketCap": 1e9, "isEtf": False},
    # AMPGR & SOXS deliberately NOT in the screener sweep
]
PREV_MAP = {
    "TSTA": {"T": "TSTA", "c": 9.0, "v": 1_000_000},
    "CLRO": {"T": "CLRO", "c": 3.22, "v": 4_000_000},
    "SCRA": {"T": "SCRA", "c": 46.5, "v": 600_000},
    # SCRB deliberately missing → screener-only row must be skipped
}


def _install_fmp_mocks(monkeypatch, *, gainers=GAINERS, losers=LOSERS, actives=ACTIVES,
                       screener=SCREENER, prev_map=PREV_MAP, calls=None):
    async def fake_get_json(url, params=None):
        if calls is not None:
            calls.append(url)
        return {
            fu.GAINERS_URL: gainers,
            fu.LOSERS_URL: losers,
            fu.ACTIVES_URL: actives,
            fu.SCREENER_URL: screener,
        }[url]

    async def fake_prev_map():
        if calls is not None:
            calls.append("polygon-grouped")
        return dict(prev_map)

    monkeypatch.setattr(fu, "_fmp_get_json", fake_get_json)
    monkeypatch.setattr(fu, "_polygon_prev_session_map", fake_prev_map)


def _poly_row(tkr, price, vol, prev_close, prev_vol):
    return {"ticker": tkr, "day": {"c": price, "v": vol},
            "prevDay": {"c": prev_close, "v": prev_vol},
            "lastTrade": {"p": price},
            "todaysChangePerc": (price - prev_close) / prev_close * 100.0}


def _grouped_maps(n=250):
    today = {f"T{i:03d}": {"T": f"T{i:03d}", "c": 10.0, "v": 2_000_000} for i in range(n)}
    prev = {f"T{i:03d}": {"T": f"T{i:03d}", "c": 9.0, "v": 1_000_000} for i in range(n)}
    return today, prev


def _fake_fmp_rows(n=220):
    return [fu._row(f"F{i:03d}", 10.0, 5_000_000, 9.0, 1_000_000, 11.11) for i in range(n)]


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Fresh caches + a deterministic env for every test."""
    fu.clear_universe_cache()
    ms._snapshot_cache["data"] = None
    ms._snapshot_cache["fetched_at"] = None
    ms._snapshot_cache.pop("source", None)
    monkeypatch.setenv("FMP_API_KEY", "test-key")
    monkeypatch.delenv("SARO_UNIVERSE", raising=False)
    monkeypatch.delenv("SARO_UNIVERSE_SHADOW", raising=False)
    yield
    fu.clear_universe_cache()
    ms._snapshot_cache["data"] = None
    ms._snapshot_cache["fetched_at"] = None
    ms._snapshot_cache.pop("source", None)


# ── universe build ──────────────────────────────────────────────────────────
def test_shape_parity_with_coarse(monkeypatch):
    """The FMP row and the equivalent Polygon-grouped row must be
    indistinguishable to funnel._coarse — same keys, IDENTICAL candidate."""
    _install_fmp_mocks(monkeypatch)
    rows = asyncio.run(fu.fetch_fmp_universe())
    by_sym = {r["ticker"]: r for r in rows}
    fmp_row = by_sym["TSTA"]

    assert set(fmp_row.keys()) == {"ticker", "day", "prevDay", "lastTrade", "todaysChangePerc"}
    assert set(fmp_row["day"].keys()) == {"c", "v"}
    assert set(fmp_row["prevDay"].keys()) == {"c", "v"}
    assert set(fmp_row["lastTrade"].keys()) == {"p"}

    poly_row = _poly_row("TSTA", 10.0, 5_000_000, 9.0, 1_000_000)
    tpl = TEMPLATES["momentum_breakout"]
    cand_fmp = _coarse(tpl, fmp_row)
    cand_poly = _coarse(tpl, poly_row)
    assert cand_fmp is not None, "FMP row must pass the flagship coarse gate"
    assert cand_fmp == cand_poly


def test_prev_close_derivation_and_change_passthrough(monkeypatch):
    _install_fmp_mocks(monkeypatch)
    rows = asyncio.run(fu.fetch_fmp_universe())
    by_sym = {r["ticker"]: r for r in rows}

    # gainer: prevClose = price - change (cross-checked live vs /stable/quote)
    assert by_sym["CLRO"]["prevDay"]["c"] == pytest.approx(6.48 - 3.26)
    assert by_sym["CLRO"]["todaysChangePerc"] == pytest.approx(101.24224)
    assert by_sym["CLRO"]["day"] == {"c": 6.48, "v": 88_538_476}
    assert by_sym["CLRO"]["prevDay"]["v"] == 4_000_000  # polygon completed session

    # loser: negative change → prevClose above price
    assert by_sym["AMPGR"]["prevDay"]["c"] == pytest.approx(0.91 + 1.22)
    assert by_sym["AMPGR"]["todaysChangePerc"] == pytest.approx(-57.277)
    # not in the screener sweep → unknown day volume stays 0 (never fabricated)
    assert by_sym["AMPGR"]["day"]["v"] == 0


def test_dedupe_by_symbol_movers_win(monkeypatch):
    _install_fmp_mocks(monkeypatch)
    rows = asyncio.run(fu.fetch_fmp_universe())
    tickers = [r["ticker"] for r in rows]
    assert len(tickers) == len(set(tickers)), "universe must be deduped by symbol"
    # TSTA appears in gainers, actives AND screener → exactly one row, and the
    # FIRST mover occurrence (gainers) wins over the actives dupe + screener
    tsta = [r for r in rows if r["ticker"] == "TSTA"]
    assert len(tsta) == 1
    assert tsta[0]["day"]["c"] == 10.0          # gainers price, not actives 10.02
    assert tsta[0]["day"]["v"] == 5_000_000     # day volume joined from screener
    # nothing is excluded here — leveraged ETFs ride through to the funnel
    assert "SOXS" in tickers


def test_screener_only_rows_join_polygon_prev(monkeypatch):
    _install_fmp_mocks(monkeypatch)
    rows = asyncio.run(fu.fetch_fmp_universe())
    by_sym = {r["ticker"]: r for r in rows}
    # screener-only symbol with a prev-map entry: prevDay {c,v} from Polygon
    assert by_sym["SCRA"]["prevDay"] == {"c": 46.5, "v": 600_000}
    assert by_sym["SCRA"]["todaysChangePerc"] == pytest.approx((50.0 - 46.5) / 46.5 * 100.0)
    # screener-only symbol WITHOUT a prev-map entry: no honest baseline → skipped
    assert "SCRB" not in by_sym


def test_universe_ttl_cache(monkeypatch):
    calls = []
    _install_fmp_mocks(monkeypatch, calls=calls)

    async def run():
        a = await fu.fetch_fmp_universe()
        b = await fu.fetch_fmp_universe()
        return a, b

    a, b = asyncio.run(run())
    assert a is b
    # one build = 4 FMP requests + 1 polygon grouped, NOT doubled by the 2nd call
    assert len(calls) == 5


def test_empty_key_returns_empty_without_requests(monkeypatch):
    calls = []
    _install_fmp_mocks(monkeypatch, calls=calls)
    monkeypatch.setenv("FMP_API_KEY", "")
    assert asyncio.run(fu.fetch_fmp_universe()) == []
    assert calls == []


# ── scanner wiring: flag routing + fallback ─────────────────────────────────
def test_default_polygon_path_never_touches_fmp(monkeypatch):
    monkeypatch.setattr(ms, "_polygon_grouped_two_days", lambda: _grouped_maps())

    async def must_not_run(*a, **k):
        raise AssertionError("fetch_fmp_universe must not be called on the default path")

    monkeypatch.setattr(fu, "fetch_fmp_universe", must_not_run)
    rows = asyncio.run(ms._fetch_market_snapshot())
    assert len(rows) == 250
    assert ms._snapshot_cache["source"] == "polygon"


def test_flag_routes_to_fmp_universe(monkeypatch):
    monkeypatch.setenv("SARO_UNIVERSE", "fmp")
    fmp_rows = _fake_fmp_rows(220)

    async def fake_fetch(*a, **k):
        return fmp_rows

    monkeypatch.setattr(fu, "fetch_fmp_universe", fake_fetch)

    def must_not_run():
        raise AssertionError("polygon path must not run when the FMP build succeeds")

    monkeypatch.setattr(ms, "_polygon_grouped_two_days", must_not_run)
    rows = asyncio.run(ms._fetch_market_snapshot())
    assert rows is fmp_rows
    assert ms._snapshot_cache["source"] == "fmp"


def test_fallback_to_polygon_when_fmp_raises(monkeypatch):
    monkeypatch.setenv("SARO_UNIVERSE", "fmp")

    async def boom(*a, **k):
        raise RuntimeError("fmp is down")

    monkeypatch.setattr(fu, "fetch_fmp_universe", boom)
    monkeypatch.setattr(ms, "_polygon_grouped_two_days", lambda: _grouped_maps())
    rows = asyncio.run(ms._fetch_market_snapshot())
    assert len(rows) == 250
    assert all(r["ticker"].startswith("T") for r in rows)
    assert ms._snapshot_cache["source"] == "polygon"


def test_fallback_to_polygon_when_fmp_too_thin(monkeypatch):
    monkeypatch.setenv("SARO_UNIVERSE", "fmp")

    async def thin(*a, **k):
        return _fake_fmp_rows(5)  # < FMP_MIN_UNIVERSE_ROWS

    monkeypatch.setattr(fu, "fetch_fmp_universe", thin)
    monkeypatch.setattr(ms, "_polygon_grouped_two_days", lambda: _grouped_maps())
    rows = asyncio.run(ms._fetch_market_snapshot())
    assert len(rows) == 250
    assert ms._snapshot_cache["source"] == "polygon"


def test_snapshot_cache_is_source_tagged(monkeypatch):
    """A fresh polygon cache must NOT be served after the env flips to fmp."""
    monkeypatch.setattr(ms, "_polygon_grouped_two_days", lambda: _grouped_maps())
    poly = asyncio.run(ms._fetch_market_snapshot())
    assert len(poly) == 250

    monkeypatch.setenv("SARO_UNIVERSE", "fmp")
    fmp_rows = _fake_fmp_rows(220)

    async def fake_fetch(*a, **k):
        return fmp_rows

    monkeypatch.setattr(fu, "fetch_fmp_universe", fake_fetch)
    rows = asyncio.run(ms._fetch_market_snapshot())
    assert rows is fmp_rows


# ── [universe-compare] parallel-validation hook ─────────────────────────────
def _capture_logs(records):
    return logger.add(lambda m: records.append(m.record["message"]), level="DEBUG")


def test_compare_hook_default_off():
    async def run():
        return fu.maybe_spawn_universe_compare([_poly_row("AAA", 10.0, 3e6, 9.0, 1e6)])

    assert asyncio.run(run()) is None


def test_compare_hook_off_when_fmp_is_live(monkeypatch):
    monkeypatch.setenv("SARO_UNIVERSE", "fmp")
    monkeypatch.setenv("SARO_UNIVERSE_SHADOW", "fmp")

    async def run():
        return fu.maybe_spawn_universe_compare([_poly_row("AAA", 10.0, 3e6, 9.0, 1e6)])

    assert asyncio.run(run()) is None


def test_compare_hook_logs_structured_line(monkeypatch):
    monkeypatch.setenv("SARO_UNIVERSE_SHADOW", "fmp")
    _install_fmp_mocks(monkeypatch)
    poly_rows = [
        _poly_row("AAA", 50.0, 1_000_000, 46.5, 300_000),   # passes momentum_breakout coarse
        _poly_row("SHRD", 60.0, 1_000_000, 56.0, 300_000),  # passes, shared with FMP set
        _poly_row("FLAT", 30.0, 1_000_000, 30.0, 900_000),  # gap 0 → funnel-excluded
    ]
    records = []
    sink = _capture_logs(records)
    try:
        async def run():
            task = fu.maybe_spawn_universe_compare(poly_rows)
            assert task is not None
            await task

        asyncio.run(run())
    finally:
        logger.remove(sink)

    lines = [r for r in records if r.startswith("[universe-compare] ")]
    assert len(lines) == 1
    payload = json.loads(lines[0][len("[universe-compare] "):])
    assert payload["polygon_rows"] == 3
    assert payload["fmp_rows"] > 0
    assert set(payload["polygon_top15_funnel"]) == {"AAA", "SHRD"}
    assert "TSTA" in payload["fmp_top15_funnel"]
    # each source's funnel picks the other doesn't even carry
    assert "TSTA" in payload["fmp_top15_missing_from_polygon_universe"]
    assert set(payload["polygon_top15_missing_from_fmp_universe"]) == {"AAA", "SHRD"}
    assert 0 <= payload["top50_dollar_vol_overlap"] <= 50
    assert "note" in payload


def test_compare_hook_throttles(monkeypatch):
    monkeypatch.setenv("SARO_UNIVERSE_SHADOW", "fmp")
    _install_fmp_mocks(monkeypatch)

    async def run():
        first = fu.maybe_spawn_universe_compare([_poly_row("AAA", 10.0, 3e6, 9.0, 1e6)])
        assert first is not None
        await first
        second = fu.maybe_spawn_universe_compare([_poly_row("AAA", 10.0, 3e6, 9.0, 1e6)])
        return second

    assert asyncio.run(run()) is None


def test_compare_hook_logs_without_raising_when_fmp_fails(monkeypatch):
    monkeypatch.setenv("SARO_UNIVERSE_SHADOW", "fmp")

    async def boom(*a, **k):
        raise RuntimeError("fmp is down")

    monkeypatch.setattr(fu, "fetch_fmp_universe", boom)
    records = []
    sink = _capture_logs(records)
    try:
        async def run():
            task = fu.maybe_spawn_universe_compare([_poly_row("AAA", 10.0, 3e6, 9.0, 1e6)])
            assert task is not None
            await task  # must NOT raise

        asyncio.run(run())
    finally:
        logger.remove(sink)
    assert any("[universe-compare]" in r and "failed" in r for r in records)


def test_compare_hook_never_raises_outside_loop():
    """Spawning without a running event loop must degrade to None, not raise."""
    import os
    os.environ["SARO_UNIVERSE_SHADOW"] = "fmp"
    try:
        assert fu.maybe_spawn_universe_compare([{"ticker": "AAA"}]) is None
    finally:
        os.environ.pop("SARO_UNIVERSE_SHADOW", None)
