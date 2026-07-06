"""TRACK fmp-universe / fmp-self-sufficiency — FMP-sourced candidate universe.

All HTTP is mocked (no live FMP calls). Coverage:
  • Shape parity: an FMP-built row and a hand-built Polygon-grouped row carry
    the same keys and produce IDENTICAL funnel._coarse candidates.
  • prevClose preference order: snapshot -> bridge EOD -> movers-derived
    (price - change) with v=0; changesPercentage passthrough.
  • ZERO Polygon inside fmp_universe: no grouped-daily URL, no
    POLYGON_API_KEY read, no _polygon_* helper — asserted on module source.
  • Dedupe by symbol across gainers/losers/actives/screener (movers win,
    day.v joined from the screener sweep; unknown volume stays 0).
  • Screener-only rows take prevDay {c,v} from the EOD snapshot; symbols
    without snapshot/bridge data are skipped.
  • BRIDGE (no snapshot yet): candidates = movers + top screener by dollar
    volume, hard cap 200 requests/build, semaphore(5), today's partial EOD
    row skipped, request count logged.
  • Full build request budgets: snapshot path = 4 HTTP + 1 snapshot read
    (5 calls through the mocked seams); bridge path <= 200 + 5.
  • 60s TTL cache: one build per TTL window.
  • Flag routing: SARO_UNIVERSE=fmp routes _fetch_market_snapshot to the FMP
    universe; default (unset/polygon) never touches FMP; the snapshot cache is
    source-tagged so an env flip can't serve the other source's rows.
  • Fallback: FMP raise OR too-thin build → Polygon path, unchanged — and the
    fallback rows cache under the REQUESTED source, so an FMP outage costs
    ONE grouped build per TTL window, not one per snapshot call.
  • Never-fabricate consumers: prevDay.v=0 rows (no completed-session
    baseline) cannot auto-pass scan_for_momentum's surge gate or the
    theta_scanner legacy gate with a phantom rel-vol.
  • [universe-compare] hook: default OFF, flag-gated, throttled, logs one
    structured line on success, logs-without-raising when FMP fails; the
    in-flight task is strongly referenced (weak-ref GC can't eat evidence)
    and a failed spawn doesn't consume the throttle.

Run: pytest backend/tests/test_fmp_universe.py -v -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest
from loguru import logger

import app.engines.data_feeds.fmp_universe as fu
import app.engines.options.momentum_scanner as ms
from app.engines.scanner.definitions import TEMPLATES
from app.engines.scanner.funnel import _coarse


@pytest.fixture(autouse=True)
def _pacing_off(monkeypatch):
    """Pin RVOL pacing OFF for every test: _paced_prev_volume scales
    prevDay.v by the WALL-CLOCK ET session fraction (0.01 overnight → 1.00 at
    the close), which made every exact prevDay.v assertion below fail when
    the suite ran outside RTH. Prev-volume assertions here are about the
    prev-map plumbing, not the pace curve — the curve has its own pinned-clock
    test (test_rvol_pacing_scales_prev_volume)."""
    monkeypatch.setattr(fu, "_PACING_ON", False)


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
# fmp_eod_snapshot.load_prev_session_map shape: {symbol: {"c", "v"}}
PREV_SNAPSHOT = {
    "TSTA": {"c": 9.0, "v": 1_000_000},
    "CLRO": {"c": 3.22, "v": 4_000_000},
    "SCRA": {"c": 46.5, "v": 600_000},
    # SCRB deliberately missing → screener-only row must be skipped
}


def _install_fmp_mocks(monkeypatch, *, gainers=GAINERS, losers=LOSERS, actives=ACTIVES,
                       screener=SCREENER, snapshot=PREV_SNAPSHOT, eod=None, calls=None):
    """Patch the two seams: _fmp_get_json (movers/screener/bridge-EOD) and
    _snapshot_prev_map (the fmp_eod_snapshot table read)."""
    eod = eod or {}

    async def fake_get_json(url, params=None, timeout_s=None):
        if url == fu.EOD_HIST_URL:
            sym = (params or {}).get("symbol")
            if calls is not None:
                calls.append(f"eod:{sym}")
            return list(eod.get(sym) or [])
        if calls is not None:
            calls.append(url)
        return {
            fu.GAINERS_URL: gainers,
            fu.LOSERS_URL: losers,
            fu.ACTIVES_URL: actives,
            fu.SCREENER_URL: screener,
        }[url]

    async def fake_snapshot_prev_map():
        if calls is not None:
            calls.append("snapshot-read")
        return dict(snapshot or {})

    monkeypatch.setattr(fu, "_fmp_get_json", fake_get_json)
    monkeypatch.setattr(fu, "_snapshot_prev_map", fake_snapshot_prev_map)


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


def _yesterday_eod_row(sym, close, volume):
    return {"symbol": sym, "date": "2000-01-03", "close": close, "volume": volume}


def _many_movers():
    """150 unique mover symbols across the three lists (50 each)."""
    def lst(prefix):
        return [{"symbol": f"{prefix}{i:03d}", "price": 10.0, "change": 1.0,
                 "changesPercentage": 11.1111, "exchange": "NASDAQ"} for i in range(50)]
    return lst("GNR"), lst("LSR"), lst("ACT")


def _many_screener(n=300):
    """n screener rows with strictly DECREASING dollar volume (S000 richest)."""
    return [{"symbol": f"S{i:03d}", "price": 10.0, "volume": (n - i) * 100_000 + 600_000,
             "marketCap": 1e9, "isEtf": False} for i in range(n)]


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


# ── zero-Polygon guarantee ──────────────────────────────────────────────────
def test_no_polygon_dependency_remains():
    """The whole point of TRACK fmp-self-sufficiency: fmp_universe must build
    with the Polygon account cancelled. No grouped-daily URL, no
    POLYGON_API_KEY read, no polygon helper — anywhere in the module."""
    src = inspect.getsource(fu)
    assert "api.polygon.io" not in src
    assert "POLYGON_API_KEY" not in src
    assert "_polygon_prev_session_map" not in src
    assert not hasattr(fu, "POLYGON_GROUPED_URL")
    assert not hasattr(fu, "_polygon_prev_session_map")


# ── universe build (snapshot present — the standing path) ───────────────────
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

    # gainer: snapshot close (3.22) == derived price - change here, both honest
    assert by_sym["CLRO"]["prevDay"]["c"] == pytest.approx(6.48 - 3.26)
    assert by_sym["CLRO"]["todaysChangePerc"] == pytest.approx(101.24224)
    assert by_sym["CLRO"]["day"] == {"c": 6.48, "v": 88_538_476}
    assert by_sym["CLRO"]["prevDay"]["v"] == 4_000_000  # snapshot completed session

    # loser NOT in the snapshot: derived prevClose = price - change (above price)
    assert by_sym["AMPGR"]["prevDay"]["c"] == pytest.approx(0.91 + 1.22)
    assert by_sym["AMPGR"]["todaysChangePerc"] == pytest.approx(-57.277)
    assert by_sym["AMPGR"]["prevDay"]["v"] == 0  # no baseline → v=0, never fabricated
    # not in the screener sweep → unknown day volume stays 0 (never fabricated)
    assert by_sym["AMPGR"]["day"]["v"] == 0


def test_snapshot_beats_derived_for_movers(monkeypatch):
    """Preference order: a snapshot entry must override the movers-derived
    prevClose — the snapshot is the REAL completed-session close."""
    snap = dict(PREV_SNAPSHOT)
    snap["CLRO"] = {"c": 3.50, "v": 7_777_777}   # ≠ derived 3.22
    _install_fmp_mocks(monkeypatch, snapshot=snap)
    rows = asyncio.run(fu.fetch_fmp_universe())
    by_sym = {r["ticker"]: r for r in rows}
    assert by_sym["CLRO"]["prevDay"] == {"c": 3.50, "v": 7_777_777}
    # changesPercentage stays FMP's own live number (passthrough)
    assert by_sym["CLRO"]["todaysChangePerc"] == pytest.approx(101.24224)


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


def test_screener_only_rows_join_snapshot_prev(monkeypatch):
    _install_fmp_mocks(monkeypatch)
    rows = asyncio.run(fu.fetch_fmp_universe())
    by_sym = {r["ticker"]: r for r in rows}
    # screener-only symbol with a snapshot entry: prevDay {c,v} from the table
    assert by_sym["SCRA"]["prevDay"] == {"c": 46.5, "v": 600_000}
    assert by_sym["SCRA"]["todaysChangePerc"] == pytest.approx((50.0 - 46.5) / 46.5 * 100.0)
    # screener-only symbol WITHOUT snapshot/bridge data: no honest baseline → skipped
    assert "SCRB" not in by_sym


def test_snapshot_path_request_budget(monkeypatch):
    """With a snapshot present the build must cost exactly 4 HTTP requests +
    1 snapshot-table read (5 calls through the seams) — ZERO bridge EOD."""
    calls = []
    _install_fmp_mocks(monkeypatch, calls=calls)
    rows = asyncio.run(fu.fetch_fmp_universe())
    assert rows
    assert len(calls) == 5
    assert not [c for c in calls if c.startswith("eod:")], "bridge must not run"


def test_universe_ttl_cache(monkeypatch):
    calls = []
    _install_fmp_mocks(monkeypatch, calls=calls)

    async def run():
        a = await fu.fetch_fmp_universe()
        b = await fu.fetch_fmp_universe()
        return a, b

    a, b = asyncio.run(run())
    assert a is b
    # one build = 4 FMP requests + 1 snapshot read, NOT doubled by the 2nd call
    assert len(calls) == 5


def test_empty_key_returns_empty_without_requests(monkeypatch):
    calls = []
    _install_fmp_mocks(monkeypatch, calls=calls)
    monkeypatch.setenv("FMP_API_KEY", "")
    assert asyncio.run(fu.fetch_fmp_universe()) == []
    assert calls == []


# ── BRIDGE (no snapshot yet — the first-morning path) ───────────────────────
def test_bridge_prev_map_when_no_snapshot(monkeypatch):
    """Snapshot table empty → per-symbol EOD bridge covers movers + screener
    extras; preference: bridge EOD -> derived (v=0); no-data screener rows
    are skipped."""
    eod = {
        "CLRO": [_yesterday_eod_row("CLRO", 3.30, 4_400_000)],
        "TSTA": [_yesterday_eod_row("TSTA", 9.0, 1_000_000)],
        "SCRA": [_yesterday_eod_row("SCRA", 46.5, 600_000)],
        # AMPGR/SOXS/SCRB: no EOD data
    }
    calls = []
    _install_fmp_mocks(monkeypatch, snapshot={}, eod=eod, calls=calls)
    rows = asyncio.run(fu.fetch_fmp_universe())
    by_sym = {r["ticker"]: r for r in rows}

    # bridge EOD beats the derived prevClose (3.30 ≠ 6.48 - 3.26)
    assert by_sym["CLRO"]["prevDay"] == {"c": 3.30, "v": 4_400_000}
    # mover with NO bridge data falls to derived prevClose + v=0
    assert by_sym["AMPGR"]["prevDay"]["c"] == pytest.approx(0.91 + 1.22)
    assert by_sym["AMPGR"]["prevDay"]["v"] == 0
    # screener-only symbol resolved by the bridge
    assert by_sym["SCRA"]["prevDay"] == {"c": 46.5, "v": 600_000}
    # screener-only symbol the bridge could not resolve stays skipped
    assert "SCRB" not in by_sym

    # every mover + every screener extra was attempted exactly once
    eod_calls = sorted(c for c in calls if c.startswith("eod:"))
    assert eod_calls == sorted(
        f"eod:{s}" for s in ["CLRO", "TSTA", "AMPGR", "SOXS", "SCRA", "SCRB"])


def test_bridge_skips_todays_partial_row(monkeypatch):
    """An EOD row dated today is a live partial session — never a baseline."""
    today = fu._today_et_datestr()
    eod = {"CLRO": [
        {"symbol": "CLRO", "date": today, "close": 6.40, "volume": 80_000_000},
        _yesterday_eod_row("CLRO", 3.30, 4_400_000),
    ]}
    _install_fmp_mocks(monkeypatch, snapshot={}, eod=eod)
    rows = asyncio.run(fu.fetch_fmp_universe())
    by_sym = {r["ticker"]: r for r in rows}
    assert by_sym["CLRO"]["prevDay"] == {"c": 3.30, "v": 4_400_000}


def test_bridge_does_not_trust_upstream_ordering(monkeypatch):
    """FMP happens to return newest-first today, but the bridge must sort by
    date itself: given rows OLDEST-first (and >5 of them, so a naive first-5
    scan would only ever see stale rows), it still picks the most recent
    completed session — not a ~10-day-old close."""
    today = fu._today_et_datestr()
    stale = [{"symbol": "CLRO", "date": f"2000-01-{d:02d}",
              "close": 1.11, "volume": 1_000} for d in range(3, 10)]  # 7 old rows
    newest = {"symbol": "CLRO", "date": "2000-01-10",
              "close": 3.30, "volume": 4_400_000}
    partial_today = {"symbol": "CLRO", "date": today,
                     "close": 6.40, "volume": 80_000_000}
    # oldest-first, newest completed row LAST, today's partial after it
    eod = {"CLRO": stale + [newest, partial_today]}
    _install_fmp_mocks(monkeypatch, snapshot={}, eod=eod)
    rows = asyncio.run(fu.fetch_fmp_universe())
    by_sym = {r["ticker"]: r for r in rows}
    assert by_sym["CLRO"]["prevDay"] == {"c": 3.30, "v": 4_400_000}


def test_bridge_cap_candidates_and_logged_count(monkeypatch):
    """150 movers + 300 screener rows → candidates are movers first, then the
    top screener rows by dollar volume, hard-capped at 200 requests; the
    request count is logged."""
    gnr, lsr, act = _many_movers()
    screener = _many_screener(300)
    eod = {}  # every request returns no data — cap accounting is what matters
    calls = []
    _install_fmp_mocks(monkeypatch, gainers=gnr, losers=lsr, actives=act,
                       screener=screener, snapshot={}, eod=eod, calls=calls)
    records = []
    sink = logger.add(lambda m: records.append(m.record["message"]), level="DEBUG")
    try:
        asyncio.run(fu.fetch_fmp_universe())
    finally:
        logger.remove(sink)

    eod_syms = [c[len("eod:"):] for c in calls if c.startswith("eod:")]
    assert len(eod_syms) == fu.BRIDGE_MAX_REQUESTS == 200
    assert len(set(eod_syms)) == 200, "no symbol fetched twice"
    # every mover made the cut...
    movers = {m["symbol"] for m in gnr + lsr + act}
    assert movers <= set(eod_syms)
    # ...and the remaining 50 slots went to the TOP screener rows by dollar vol
    assert set(eod_syms) - movers == {f"S{i:03d}" for i in range(50)}
    assert any("bridge EOD backfill: 200 requests" in r for r in records)


def test_bridge_semaphore_limits_concurrency(monkeypatch):
    """No more than BRIDGE_CONCURRENCY (5) EOD fetches in flight at once."""
    gnr, lsr, act = _many_movers()
    state = {"inflight": 0, "max": 0}

    async def fake_get_json(url, params=None, timeout_s=None):
        if url == fu.EOD_HIST_URL:
            state["inflight"] += 1
            state["max"] = max(state["max"], state["inflight"])
            await asyncio.sleep(0.001)
            state["inflight"] -= 1
            return [_yesterday_eod_row(params["symbol"], 9.0, 1_000_000)]
        return {fu.GAINERS_URL: gnr, fu.LOSERS_URL: lsr,
                fu.ACTIVES_URL: act, fu.SCREENER_URL: []}[url]

    async def no_snapshot():
        return {}

    monkeypatch.setattr(fu, "_fmp_get_json", fake_get_json)
    monkeypatch.setattr(fu, "_snapshot_prev_map", no_snapshot)
    rows = asyncio.run(fu.fetch_fmp_universe())
    assert len(rows) == 150
    assert 1 <= state["max"] <= fu.BRIDGE_CONCURRENCY == 5


def test_bridge_path_full_build_within_budget(monkeypatch):
    """First morning (no snapshot): the whole universe still builds, above the
    scanner's row floor, within 200 bridge + 4 list requests (+1 snapshot
    read) — and every row carries a real completed-session prevDay."""
    gnr, lsr, act = _many_movers()
    screener = _many_screener(300)
    eod = {s: [_yesterday_eod_row(s, 9.0, 1_000_000)]
           for s in ({m["symbol"] for m in gnr + lsr + act} |
                     {r["symbol"] for r in screener})}
    calls = []
    _install_fmp_mocks(monkeypatch, gainers=gnr, losers=lsr, actives=act,
                       screener=screener, snapshot={}, eod=eod, calls=calls)
    rows = asyncio.run(fu.fetch_fmp_universe())

    assert len(calls) <= 200 + 5
    assert len(rows) >= fu.FMP_MIN_UNIVERSE_ROWS
    by_sym = {r["ticker"]: r for r in rows}
    # movers resolved via bridge; capped-out screener rows (S050+) skipped
    assert by_sym["GNR000"]["prevDay"] == {"c": 9.0, "v": 1_000_000}
    assert "S000" in by_sym and "S299" not in by_sym


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
    # the cache is tagged with the REQUESTED source so the fallback rows
    # actually serve from cache while the flag stays fmp (review finding)
    assert ms._snapshot_cache["source"] == "fmp"


def test_fallback_to_polygon_when_fmp_too_thin(monkeypatch):
    monkeypatch.setenv("SARO_UNIVERSE", "fmp")

    async def thin(*a, **k):
        return _fake_fmp_rows(5)  # < FMP_MIN_UNIVERSE_ROWS

    monkeypatch.setattr(fu, "fetch_fmp_universe", thin)
    monkeypatch.setattr(ms, "_polygon_grouped_two_days", lambda: _grouped_maps())
    rows = asyncio.run(ms._fetch_market_snapshot())
    assert len(rows) == 250
    assert ms._snapshot_cache["source"] == "fmp"  # requested source — see above


def test_fmp_fallback_rows_cache_under_requested_source(monkeypatch):
    """CONFIRMED review finding: under SARO_UNIVERSE=fmp with a thin/failing
    FMP build, the polygon fallback rows were cached with source="polygon",
    which never matches the requested "fmp" on read — so EVERY snapshot call
    re-ran the two heavy grouped-daily fetches (17 calls per funnel cycle).
    The fallback must be cached under the requested source: back-to-back
    calls = ONE grouped build."""
    monkeypatch.setenv("SARO_UNIVERSE", "fmp")
    calls = {"fmp": 0, "grouped": 0}

    async def thin(*a, **k):
        calls["fmp"] += 1
        return _fake_fmp_rows(5)  # < FMP_MIN_UNIVERSE_ROWS

    def grouped():
        calls["grouped"] += 1
        return _grouped_maps()

    monkeypatch.setattr(fu, "fetch_fmp_universe", thin)
    monkeypatch.setattr(ms, "_polygon_grouped_two_days", grouped)

    async def run():
        first = await ms._fetch_market_snapshot()
        again = [await ms._fetch_market_snapshot() for _ in range(16)]
        return first, again

    first, again = asyncio.run(run())
    assert len(first) == 250
    assert all(r is first for r in again), "later calls must serve the cache"
    assert calls["grouped"] == 1, "one grouped build per TTL window, not per call"
    assert calls["fmp"] == 1, "FMP retried once per TTL window, not per call"


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


# ── never-fabricate: prevDay.v=0 must not auto-pass rel-vol gates ───────────
def _no_baseline_rows():
    """A fabricated-baseline monster (prevDay.v=0 — e.g. an FMP mover missing
    from the snapshot/bridge prev map) next to a genuine 5x mover."""
    newipo = _poly_row("NEWIPO", 8.0, 8_000_000, 6.96, 0)
    newipo["todaysChangePerc"] = 15.0
    real = _poly_row("REAL", 5.0, 5_000_000, 4.35, 1_000_000)
    real["todaysChangePerc"] = 15.0
    return [newipo, real]


def test_scan_for_momentum_never_fabricates_rel_vol(monkeypatch):
    """CONFIRMED review finding: `int(prev.get("v") or 0) or 1` turned
    prevDay.v=0 rows into auto-passing hits with a phantom vol_ratio (probed
    8,000,000x). No completed-session baseline → no hit; the genuine mover
    still passes."""
    rows = _no_baseline_rows()

    async def fake_snapshot(*a, **k):
        return rows

    monkeypatch.setattr(ms, "_fetch_market_snapshot", fake_snapshot)
    hits = asyncio.run(ms.scan_for_momentum())
    assert [h.ticker for h in hits] == ["REAL"]
    assert hits[0].pct_of_avg_volume == pytest.approx(5.0)


def test_theta_legacy_gate_never_fabricates_rel_vol(monkeypatch):
    """Same hole in theta_scanner's legacy path: `prev_vol > 0 and ratio < 2.5`
    skipped the surge gate at prev_vol=0, then max(prev_vol, 1) granted the
    max rel_vol score multiplier. A fabricated-baseline row must not even
    become a candidate (previously it scored past MIN_SCORE=15)."""
    import app.engines.options.theta_scanner as ts
    import app.engines.scanner.definitions as defs

    newipo = _poly_row("NEWIPO", 20.0, 5_000_000, 18.0, 0)  # gap 11%, $100M traded

    async def fake_snapshot(*a, **k):
        return [newipo]

    catalyst_calls = []

    async def fake_catalyst(db, ticker):
        catalyst_calls.append(ticker)
        return 1.0, ""

    monkeypatch.setattr(defs, "enabled_templates", lambda: {})  # force legacy path
    monkeypatch.setattr(ms, "_fetch_market_snapshot", fake_snapshot)
    monkeypatch.setattr(ts, "_get_8k_catalyst", fake_catalyst)

    pick = asyncio.run(ts.find_best_premarket_pick(db=None))
    assert pick is None
    assert catalyst_calls == [], "fabricated-baseline row must be gated before scoring"
    assert ts._NOPICK_STATE["last"]["reason"] == "no gapper met the universe filters today"


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


def test_compare_task_strong_ref_held_until_done(monkeypatch):
    """Review finding: asyncio keeps only WEAK task refs and momentum_scanner
    discards the hook's return value, so without a module-level strong ref the
    compare task could be GC'd mid-flight and the [universe-compare] evidence
    line silently lost. The ref must live exactly as long as the task."""
    monkeypatch.setenv("SARO_UNIVERSE_SHADOW", "fmp")
    _install_fmp_mocks(monkeypatch)

    async def run():
        task = fu.maybe_spawn_universe_compare([_poly_row("AAA", 10.0, 3e6, 9.0, 1e6)])
        assert task is not None
        assert task in fu._compare_tasks, "strong ref must be held while in flight"
        await task
        for _ in range(3):
            await asyncio.sleep(0)  # let the done-callback fire
        assert task not in fu._compare_tasks, "ref must be discarded on completion"

    asyncio.run(run())


def test_compare_hook_never_raises_outside_loop():
    """Spawning without a running event loop must degrade to None, not raise —
    and the failed spawn must NOT consume the 300s throttle window."""
    import os
    os.environ["SARO_UNIVERSE_SHADOW"] = "fmp"
    try:
        assert fu.maybe_spawn_universe_compare([{"ticker": "AAA"}]) is None
        assert fu._compare_last_mono == 0.0, "failed spawn must not burn the throttle"
    finally:
        os.environ.pop("SARO_UNIVERSE_SHADOW", None)


# ── RVOL pacing (review finding 4) — pinned clock, no wall-time flake ────────
def test_rvol_pacing_scales_prev_volume(monkeypatch):
    """_paced_prev_volume scales the prev-session denominator by the expected
    intraday volume fraction so rel_vol reads vs-pace. Pinned to fixed ET
    clock points — the ONLY test allowed to exercise the pace curve (all other
    tests pin pacing off via the autouse fixture)."""
    monkeypatch.setattr(fu, "_PACING_ON", True)

    def _at(et_minutes):
        monkeypatch.setattr(fu, "_now_et_minutes", lambda: et_minutes)
        return fu._paced_prev_volume(1_000_000)

    assert _at(16 * 60) == 1_000_000          # close -> full prev session
    assert _at(20 * 60) == 1_000_000          # after hours -> stays full
    assert _at(2 * 60) == 10_000              # overnight floor = 0.01
    assert _at(10 * 60) == 210_000            # 10:00 ET curve point = 0.21
    # 10:30 ET sits halfway between the 0.21 and 0.33 anchors -> 0.27.
    assert _at(10 * 60 + 30) == 270_000
    # Guards: junk/zero are passthrough, and the paced value never hits 0.
    assert fu._paced_prev_volume(None) == 0
    assert fu._paced_prev_volume(0) == 0
    monkeypatch.setattr(fu, "_now_et_minutes", lambda: 2 * 60)
    assert fu._paced_prev_volume(5) == 1      # max(1, ...) floor

    monkeypatch.setattr(fu, "_PACING_ON", False)
    monkeypatch.setattr(fu, "_now_et_minutes", lambda: 2 * 60)
    assert fu._paced_prev_volume(1_000_000) == 1_000_000  # kill-switch: raw
