"""SARO_PICK_ENGINE compression live-pick tests (money path).

Covers, per the owner-approved replacement spec:
  * dispatch honors SARO_PICK_ENGINE for BOTH values ('compression' default
    -> saro13 live engine; 'momentum' -> legacy path untouched), at the
    single seam inside theta_scanner.find_best_premarket_pick (both
    scheduler call sites inherit it; premarket_scheduler.py is unmodified);
  * the compression pick carries the EXACT legacy emit field set —
    field-for-field against a RECORDED legacy call (prod redis
    theta:lastpick:latest, captured 2026-08-18) — and survives an
    access-for-access probe of every read emit_theta_pick + the scanner
    job perform (subscripts AND format specs);
  * the Section-2 alert legs (squeeze-on-prior-bar, upper-Keltner break,
    day RVOL >= 4 pro-rata) are each INDIVIDUALLY necessary on synthetic
    1-min bars;
  * no alert => no pick + the existing no-pick machinery is reachable with
    an honest compression-specific reason;
  * M&A hard-exclude is active on the live path (inherited from
    screen.run_screen — polarity pinned, single-candidate-source pinned);
  * budget caps: <= 10 intraday bar fetches per tick, the screen runs once
    per day (+ exactly one 9:30 retry iff the premarket build was empty);
  * latch-burn protection: the alerted pick is cached by ET day and
    returned VERBATIM on the re-call even when fresh bars would miss;
  * the stale-quote gate (>3% screener-vs-alert-close) hard-skips;
  * legacy path untouched except the dispatch seam (source-scope pins);
  * the saro12/saro13 constants the live path couples to are pinned.

Runs under pytest OR directly:  python tests/test_live_pick_compression.py
(email env is never touched: nothing here imports app.services.email).
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, patch

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from app.engines.options import theta_scanner as TS  # noqa: E402
from app.engines.scanner.saro12 import config as C12  # noqa: E402
from app.engines.scanner.saro12.indicators import alert_state, bars_to_df  # noqa: E402
from app.engines.scanner.saro13 import config as C13  # noqa: E402
from app.engines.scanner.saro13 import live_pick as LP  # noqa: E402
from app.engines.scanner.saro13 import screen as SCREEN  # noqa: E402

DATE = "2026-08-18"                      # a Tuesday
T_945 = datetime(2026, 8, 18, 9, 45)     # in-window, 1-min cadence band
T_800 = datetime(2026, 8, 18, 8, 0)      # premarket
T_950 = datetime(2026, 8, 18, 9, 50)

NOW_ET = "app.engines.options.ignition_shadow._now_et"
RUN_SCREEN = "app.engines.scanner.saro13.screen.run_screen"
FETCH_BARS = "app.engines.data_feeds.fmp_feed.fetch_intraday_bars"


# ── RECORDED LEGACY CALL (prod redis theta:lastpick:latest, 2026-08-18,
#    funnel pick BABA — the exact dict emit_theta_pick consumed that day) ──
RECORDED_LEGACY_PICK = {
    "ticker": "BABA", "price": 128.42, "gap_pct": 3.0, "rel_vol": 2.08,
    "today_vol": 2232210.0, "dollar_vol": 286660408.2, "score": 51.4,
    "matched_strategy": "premarket_gap_continuation", "live_price": 128.55,
    "prev_close_daily": 119.7, "chg_5d_pct": 7.87, "chg_20d_pct": 7.7,
    "dist_from_60d_high_pct": -9.71, "adv20_shares": 14638368,
    "adv20_dollars": 1523809963, "rel_vol_adv20": 0.15, "former_runner_days": 0,
    "prior_day_ret_pct": 3.74, "prior_day_clv": 0.328, "live_gap_pct": 7.39,
    "analyst_upside_pct": 43.91, "analyst_rating": "Buy",
    "catalyst_reason": "multi-strategy match",
    "quality_reasons": ["gap 3%", "rel-vol 2.08x", "catalyst: multi-strategy match",
                        "$-vol $67.05M", "above VWAP (+0.2%)", "consolidating"],
    "watch_only": False, "entry": 128.55, "stop": 126.62, "target": 132.02,
    "projected_move_pct": 2.7,
    "stop_reason": "1.5% ATR-proxy stop (no clean structure in range)",
    "target_reason": "measured move 1.8R from 1.5% ATR-proxy stop (no clean structure in range)",
    "rr": 1.8, "levels_basis": "atr_fallback", "stop_is_placeholder": True,
    "asset_type": "options",
    "alternatives": [{"ticker": "FCEL", "score": 47.5, "gap_pct": 8.6}],
    "picked_at": "2026-08-18T13:38:49.416722+00:00",
}
# Funnel-enrichment extras NOT part of the emit/scheduler/widget contract
# (never read by emit_theta_pick, _check_and_run_theta_scanner, or the
# pending-entry payload) — everything else in the recorded call IS contract.
_ENRICHMENT_ONLY = {
    "dollar_vol", "prev_close_daily", "chg_5d_pct", "chg_20d_pct",
    "dist_from_60d_high_pct", "adv20_shares", "adv20_dollars", "rel_vol_adv20",
    "former_runner_days", "prior_day_ret_pct", "prior_day_clv", "live_gap_pct",
    "analyst_upside_pct", "analyst_rating", "picked_at",
}
CONTRACT_FIELDS = set(RECORDED_LEGACY_PICK) - _ENRICHMENT_ONLY


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _reset():
    LP._MEM["pick"] = {}
    LP._MEM["cands"] = {}
    LP._MEM["refresh930"] = set()
    TS._NOPICK_STATE["last"] = None


class FakeRedis:
    """Minimal async redis stub (get/set/setex/aclose) for the day caches."""
    def __init__(self):
        self.store: dict = {}

    async def get(self, k):
        return self.store.get(k)

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in self.store:
            return None
        self.store[k] = v
        return True

    async def setex(self, k, ttl, v):
        self.store[k] = v
        return True

    async def aclose(self):
        return None


# ── synthetic data builders ────────────────────────────────────────────────
def _mk_cand(ticker="COIL", price=10.45, **over) -> dict:
    """A run_screen-shaped candidate (field names from screen.run_screen)."""
    cand = {
        "ticker": ticker, "price": price, "today_vol": 500_000,
        "close": 10.40, "prev_close": 10.35, "live_chg_pct": 0.48,
        "upper_bb": 10.55, "lower_bb": 10.15, "bb_width": 0.038,
        "swing_low": 10.05, "compression_range": 0.40,
        "rsi": 51.2, "atr": 0.21, "vol_ratio": 0.8,
        "screen_reasons": [
            "BB width 0.038 <= p15 0.045 of trailing 60d (lowest-15% coil)",
            "no pending M&A evidence (targetedSymbol list + headlines clean)",
        ],
        "comp_detail": {"bb_width": 0.038, "bb_width_thresh": 0.045},
    }
    cand.update(over)
    return cand


def _coil_bars(n=60, base=10.45, last_close=10.60, vol=5_000) -> list:
    """Tight 1-min coil (BB inside KC -> squeeze ON) ending in a bar that
    closes at `last_close`. last_close=10.60 breaks the upper Keltner
    (~10.52 after the update); last_close=10.44 stays inside (no breakout)."""
    bars = []
    for i in range(n - 1):
        c = base + (0.01 if i % 2 == 0 else -0.01)
        bars.append({"t": i, "o": c, "h": c + 0.01, "l": c - 0.01, "c": c, "v": vol})
    hi = max(last_close + 0.02, base + 0.01)
    bars.append({"t": n - 1, "o": base, "h": hi, "l": base - 0.01,
                 "c": last_close, "v": vol})
    return bars


def _ramp_dip_break_bars(vol=5_000) -> list:
    """Trending ramp (BB far OUTSIDE KC -> squeeze OFF on every bar), then a
    deep dip bar (close well under the upper Keltner) and a violent
    recovery bar closing back above it: breakout leg TRUE, squeeze_prev
    FALSE — isolates the squeeze leg."""
    bars = []
    c = 10.0
    for i in range(50):
        c = 10.0 + 0.3 * i
        bars.append({"t": i, "o": c, "h": c + 0.05, "l": c - 0.05, "c": c, "v": vol})
    bars.append({"t": 50, "o": c, "h": c, "l": c - 7.2, "c": c - 7.0, "v": vol})
    bars.append({"t": 51, "o": c - 7.0, "h": c + 1.6, "l": c - 7.0,
                 "c": c + 1.5, "v": vol})
    return bars


def _seed_daily_cache(r: FakeRedis, ticker: str, n=70, vol=1_000_000):
    """~3 months of completed EOD sessions in the saro13 day-cache (the RVOL
    denominator source). Dates strictly before DATE."""
    rows = [{"date": f"2026-{4 + i // 28:02d}-{1 + i % 28:02d}",
             "open": 10, "high": 10.5, "low": 9.8, "close": 10.2, "volume": vol}
            for i in range(n)]
    r.store[C13.DAILY_CACHE_FMT.format(date=DATE, ticker=ticker)] = json.dumps(rows)


async def _call(now, r, db=None):
    return await LP.find_compression_pick(db, redis_client=r)


def _pick_via(now, cands, bars_map, r=None):
    """Standard harness: fixed clock, mocked screen + intraday bars."""
    r = r or FakeRedis()
    for c in cands:
        _seed_daily_cache(r, c["ticker"])

    async def _fetch(sym, **kw):
        return bars_map.get(sym, [])

    with patch(NOW_ET, return_value=now), \
         patch(RUN_SCREEN, new=AsyncMock(return_value=cands)) as rs, \
         patch(FETCH_BARS, new=AsyncMock(side_effect=_fetch)) as fb:
        pick = _run(_call(now, r))
    return pick, r, rs, fb


# ═══ 1. dispatch honors SARO_PICK_ENGINE (both values) ════════════════════
def test_dispatch_default_and_explicit_compression():
    _reset()
    sentinel = {"ticker": "SENTINEL-C"}
    for env in (None, "compression"):
        with patch.dict(os.environ):
            os.environ.pop("SARO_PICK_ENGINE", None)
            if env is not None:
                os.environ["SARO_PICK_ENGINE"] = env
            with patch("app.engines.scanner.saro13.live_pick.find_compression_pick",
                       new=AsyncMock(return_value=sentinel)) as comp, \
                 patch("app.engines.options.theta_scanner.find_best_pick_via_funnel",
                       new=AsyncMock()) as funnel:
                out = _run(TS.find_best_premarket_pick(None))
        assert out == sentinel, f"env={env!r}: compression engine not dispatched"
        assert comp.await_count == 1
        funnel.assert_not_awaited()


def test_dispatch_momentum_runs_legacy_untouched():
    _reset()
    legacy = {"ticker": "SENTINEL-M", "score": 42}
    with patch.dict(os.environ, {"SARO_PICK_ENGINE": "momentum"}):
        with patch("app.engines.scanner.saro13.live_pick.find_compression_pick",
                   new=AsyncMock()) as comp, \
             patch("app.engines.scanner.definitions.enabled_templates",
                   return_value=[object()]), \
             patch("app.engines.options.theta_scanner.find_best_pick_via_funnel",
                   new=AsyncMock(return_value=legacy)) as funnel:
            out = _run(TS.find_best_premarket_pick(None))
    assert out == legacy, "momentum env must run the legacy path"
    assert funnel.await_count == 1
    comp.assert_not_awaited()


# ═══ 2. exact legacy field set (field-for-field vs the recorded call) ═════
def _jtype(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "num"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def _emit_contract_probe(pick: dict):
    """Every read emit_theta_pick + _check_and_run_theta_scanner + the
    pending-entry payload perform on the pick — subscripts AND format
    specs. A missing/None/mistyped field fails here exactly where prod
    would (theta_scanner.py emit body + premarket_scheduler job body)."""
    qty = max(1, int(1000 / pick["price"]))                     # emit sizing
    assert qty >= 1
    _ = f"+{pick['projected_move_pct']:.0f}% target"            # subject
    _ = f"${pick['entry']:.2f}"; _ = f"${pick['stop']:.2f}"     # email table
    _ = f"${pick['target']:.2f}"
    _ = f"{(pick['stop'] / pick['entry'] - 1) * 100:+.1f}%"
    _ = f"{(pick['target'] / pick['entry'] - 1) * 100:+.1f}%"
    _ = f"+{pick['gap_pct']:.1f}%"                              # Gap row
    _ = f"{pick['rel_vol']}×"                                   # Volume row
    _ = str(pick["catalyst_reason"]); _ = str(pick["score"])    # rows
    for a in pick.get("alternatives", [])[:3]:                  # runners-up
        _ = f"{a['ticker']} (gap {a['gap_pct']}%)"
    _hist = {"tk": pick["ticker"], "at": pick.get("asset_type", "options"),
             "en": pick["entry"], "st": pick["stop"], "tg": pick["target"],
             "gp": pick["gap_pct"], "rv": pick.get("rel_vol", 0),
             "tv": pick["today_vol"], "sc": pick["score"],
             "cr": pick["catalyst_reason"],
             "qr": json.dumps(pick.get("quality_reasons", []))}  # history INSERT
    assert _hist["tv"] is not None
    _ = float(pick.get("entry") or pick["price"])               # pending-entry basis
    _ = float(pick.get("live_price") or 0)                      # _entry_basis
    assert float(pick.get("score", 0) or 0) >= 10.0             # job threshold 9:33-12
    assert bool(pick.get("watch_only")) is False                # job fire gate
    assert pick["stop"] < pick["entry"] < pick["target"]        # level geometry
    json.dumps(pick, default=str)                               # redis-serializable


def test_compression_pick_matches_recorded_legacy_field_set():
    _reset()
    cand = _mk_cand()
    pick, r, _, _ = _pick_via(T_945, [cand], {"COIL": _coil_bars()})
    assert pick is not None, "the synthetic coil breakout must alert"
    missing = [f for f in CONTRACT_FIELDS if f not in pick]
    assert not missing, f"legacy contract fields missing from compression pick: {missing}"
    for f in CONTRACT_FIELDS:
        assert _jtype(pick[f]) == _jtype(RECORDED_LEGACY_PICK[f]), \
            f"field {f!r}: type {_jtype(pick[f])} != recorded {_jtype(RECORDED_LEGACY_PICK[f])}"
    for a in pick["alternatives"]:
        assert set(a) == {"ticker", "score", "gap_pct"}  # recorded alt shape
    _emit_contract_probe(pick)
    # live-price entry basis: entry IS the alert-bar close, executable now
    assert pick["entry"] == 10.60 and pick["price"] == 10.60 and pick["live_price"] == 10.60
    # structural stop kept (coil floor below live entry), measured-move target
    assert pick["stop"] == 10.15
    assert abs(pick["target"] - (10.60 + C13.TARGET_RANGE_MULT * 0.40)) < 1e-9
    # score convention: 10 + RVOL >= 14 — deterministically clears threshold 10
    assert pick["score"] >= 14.0
    # the pick was cached for the post-latch re-call BEFORE being returned
    assert r.store.get(LP.PICK_KEY_FMT.format(date=DATE))
    assert r.store.get("theta:lastpick:latest")  # frontend widget parity


# ═══ 3. alert legs individually necessary on synthetic bars ═══════════════
def test_alert_legs_each_individually_necessary():
    _reset()
    df = bars_to_df(_coil_bars())
    st = alert_state(df, 6.0)
    assert st["squeeze_prev"] and st["breakout"] and st["rvol_ok"] and st["alert"], \
        f"base synthetic coil must alert: {st}"
    # (a) RVOL leg alone removed -> no alert
    st_rv = alert_state(df, C12.RVOL_MIN - 0.1)
    assert st_rv["squeeze_prev"] and st_rv["breakout"] and not st_rv["rvol_ok"]
    assert not st_rv["alert"], "RVOL leg must be individually necessary"
    # (b) breakout leg alone removed (last bar stays inside the coil)
    st_bk = alert_state(bars_to_df(_coil_bars(last_close=10.44)), 6.0)
    assert st_bk["squeeze_prev"] and not st_bk["breakout"] and st_bk["rvol_ok"]
    assert not st_bk["alert"], "KC-breakout leg must be individually necessary"
    # (c) squeeze-prior-bar leg alone removed (trend blows the bands; the
    #     final bar still crosses the upper Keltner)
    st_sq = alert_state(bars_to_df(_ramp_dip_break_bars()), 6.0)
    assert st_sq["breakout"] and st_sq["rvol_ok"] and not st_sq["squeeze_prev"]
    assert not st_sq["alert"], "squeeze-prior-bar leg must be individually necessary"


def test_no_breakout_end_to_end_means_no_pick():
    _reset()
    pick, _, _, fb = _pick_via(T_945, [_mk_cand()], {"COIL": _coil_bars(last_close=10.44)})
    assert pick is None
    assert fb.await_count == 1  # it DID look at the bars — the alert just isn't there


# ═══ 4. no alert => no emit; no-pick machinery reachable + honest ═════════
def test_no_alert_sets_honest_compression_nopick_reason():
    _reset()
    pick, _, _, _ = _pick_via(T_945, [_mk_cand(), _mk_cand(ticker="WIND")],
                              {"COIL": _coil_bars(last_close=10.44), "WIND": []})
    assert pick is None
    last = getattr(TS, "_NOPICK_STATE", {}).get("last")  # scheduler guard's exact read
    assert last and "reason" in last
    assert "COIL" in last["reason"] and "WIND" in last["reason"]
    assert "rare by design" in last["reason"], "expectation-setting must reach the email"


def test_zero_candidates_sets_honest_nopick_reason():
    _reset()
    pick, _, _, fb = _pick_via(T_945, [], {})
    assert pick is None
    fb.assert_not_awaited()
    last = TS._NOPICK_STATE["last"]
    assert last and "0 coiled candidates" in last["reason"]
    assert "rare by design" in last["reason"]


# ═══ 5. M&A hard-exclude active on the live path ══════════════════════════
def test_ma_gate_polarity_pinned():
    _reset()
    r = FakeRedis()
    budget = {"eod_pulls": 0, "news_checks": 0, "cap_hit": False}
    # confirmed targetedSymbol -> fail CLOSED (hard exclude)
    with patch("app.engines.scanner.saro13.screen._ma_list",
               new=AsyncMock(return_value=[{"targetedSymbol": "COIL"}])):
        excluded, note = _run(SCREEN.ma_gate(r, DATE, "COIL", budget))
    assert excluded and "HARD EXCLUDE" in note
    # headline keyword fallback -> fail CLOSED
    with patch("app.engines.scanner.saro13.screen._ma_list",
               new=AsyncMock(return_value=[])), \
         patch("app.engines.scanner.saro13.screen._news_headlines",
               new=AsyncMock(return_value=[{"title": "Coil Corp agrees to merger"}])):
        excluded, note = _run(SCREEN.ma_gate(r, DATE, "COIL", budget))
    assert excluded and "HARD EXCLUDE" in note
    # BOTH sources unavailable -> fail OPEN (outage must not zero the list)
    with patch("app.engines.scanner.saro13.screen._ma_list",
               new=AsyncMock(return_value=None)), \
         patch("app.engines.scanner.saro13.screen._news_headlines",
               new=AsyncMock(return_value=None)):
        excluded, note = _run(SCREEN.ma_gate(r, DATE, "COIL", budget))
    assert not excluded and "fail-open" in note


def test_live_path_single_candidate_source_is_run_screen():
    """The live engine can only ever pick from run_screen output — the
    funnel that applies the M&A hard exclude. An M&A name never reaches
    the candidate list, so it can never be picked."""
    _reset()
    src = inspect.getsource(LP)
    assert "run_screen" in src
    assert "momentum_scanner" not in src, "no alternate candidate source module"
    assert "_fetch_market_snapshot" not in src
    assert "fmp_universe" not in src, "market data only via run_screen / fetch_intraday_bars"
    scr = inspect.getsource(SCREEN.run_screen)
    assert "ma_gate" in scr, "run_screen must apply the M&A gate"
    # end-to-end: the picked ticker comes from run_screen's return, only
    cand = _mk_cand(ticker="CLEAN")
    pick, _, rs, _ = _pick_via(T_945, [cand], {"CLEAN": _coil_bars()})
    assert rs.await_count == 1
    assert pick and pick["ticker"] == "CLEAN"


# ═══ 6. budget caps ═══════════════════════════════════════════════════════
def test_budget_caps_10_fetches_per_tick_and_one_screen_per_day():
    _reset()
    cands = [_mk_cand(ticker=f"T{i:02d}") for i in range(15)]
    r = FakeRedis()

    async def _fetch(sym, **kw):
        return []  # no bars -> no alert, but the call still counts

    with patch(NOW_ET, return_value=T_945), \
         patch(RUN_SCREEN, new=AsyncMock(return_value=cands)) as rs, \
         patch(FETCH_BARS, new=AsyncMock(side_effect=_fetch)) as fb:
        assert _run(_call(T_945, r)) is None
        assert fb.await_count == LP.INTRADAY_TICKER_CAP == 10, \
            "intraday alert checks must cap at 10 tickers/tick"
        assert rs.await_count == 1
        # second tick: candidate list comes from the day cache, screen NOT re-run
        assert _run(_call(T_950, r)) is None
        assert rs.await_count == 1, "screen must run once per day (day-cached)"
        assert fb.await_count == 20


def test_premarket_builds_candidates_but_fetches_no_bars():
    _reset()
    r = FakeRedis()
    with patch(NOW_ET, return_value=T_800), \
         patch(RUN_SCREEN, new=AsyncMock(return_value=[_mk_cand()])) as rs, \
         patch(FETCH_BARS, new=AsyncMock()) as fb:
        assert _run(_call(T_800, r)) is None
    assert rs.await_count == 1, "premarket tick must build the day's list"
    fb.assert_not_awaited()
    assert r.store.get(LP.CANDS_KEY_FMT.format(date=DATE)), "list must be day-cached"


def test_930_refresh_exactly_once_when_premarket_list_empty():
    _reset()
    r = FakeRedis()
    results = [[], [_mk_cand()], AssertionError("screen must not run a 3rd time")]

    async def _screen(**kw):
        nxt = results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    with patch(RUN_SCREEN, new=AsyncMock(side_effect=_screen)) as rs, \
         patch(FETCH_BARS, new=AsyncMock(return_value=[])):
        with patch(NOW_ET, return_value=T_800):
            assert _run(_call(T_800, r)) is None       # premarket build -> empty
        assert rs.await_count == 1
        with patch(NOW_ET, return_value=T_945):
            assert _run(_call(T_945, r)) is None       # 9:30+ empty -> ONE retry
        assert rs.await_count == 2
        with patch(NOW_ET, return_value=T_950):
            assert _run(_call(T_950, r)) is None       # list now cached non-empty
        assert rs.await_count == 2


# ═══ 7. latch-burn protection: deterministic re-call ══════════════════════
def test_alerted_pick_cached_and_returned_verbatim_on_recall():
    _reset()
    r = FakeRedis()
    pick, r, _, _ = _pick_via(T_945, [_mk_cand()], {"COIL": _coil_bars()}, r=r)
    assert pick is not None
    # the re-call happens AFTER theta_fired is burned; fresh bars may miss
    # the alert — the cached pick must come back VERBATIM with no rescans
    with patch(NOW_ET, return_value=T_950), \
         patch(RUN_SCREEN, new=AsyncMock(side_effect=AssertionError("no rescan"))), \
         patch(FETCH_BARS, new=AsyncMock(side_effect=AssertionError("no refetch"))):
        again = _run(_call(T_950, r))
    assert again == pick, "re-call must return the alerted pick verbatim"
    # redis blip between the two calls: the in-process cache still serves it
    with patch(NOW_ET, return_value=T_950), \
         patch(RUN_SCREEN, new=AsyncMock(side_effect=AssertionError("no rescan"))), \
         patch(FETCH_BARS, new=AsyncMock(side_effect=AssertionError("no refetch"))):
        again2 = _run(_call(T_950, FakeRedis()))
    assert again2 == pick
    # process-restart simulation: memory wiped, redis survives
    LP._MEM["pick"] = {}
    LP._MEM["cands"] = {}
    with patch(NOW_ET, return_value=T_950), \
         patch(RUN_SCREEN, new=AsyncMock(side_effect=AssertionError("no rescan"))), \
         patch(FETCH_BARS, new=AsyncMock(side_effect=AssertionError("no refetch"))):
        again3 = _run(_call(T_950, r))
    assert again3 == pick


# ═══ 8. stale-quote gate on the live path ═════════════════════════════════
def test_stale_quote_gate_hard_skips_over_3pct_divergence():
    _reset()
    # alert close 10.60 vs morning screener price 10.00 = +6% -> HARD skip
    pick, _, _, _ = _pick_via(T_945, [_mk_cand(price=10.00)], {"COIL": _coil_bars()})
    assert pick is None, ">3% screener-vs-alert divergence must not emit"
    _reset()
    # 10.45 -> 10.60 = +1.4% -> passes
    pick, _, _, _ = _pick_via(T_945, [_mk_cand(price=10.45)], {"COIL": _coil_bars()})
    assert pick is not None


# ═══ 9. RVOL denominator fails closed without the EOD cache ═══════════════
def test_missing_eod_cache_means_rvol_unknown_and_no_alert():
    _reset()
    r = FakeRedis()  # NOTE: daily cache NOT seeded

    async def _fetch(sym, **kw):
        return _coil_bars()

    with patch(NOW_ET, return_value=T_945), \
         patch(RUN_SCREEN, new=AsyncMock(return_value=[_mk_cand()])), \
         patch(FETCH_BARS, new=AsyncMock(side_effect=_fetch)):
        assert _run(_call(T_945, r)) is None, "no RVOL denominator -> leg fails closed"


# ═══ 10. legacy path untouched except the dispatch seam ═══════════════════
def test_dispatch_seam_scope_is_minimal_and_single_site():
    src_ts = inspect.getsource(TS)
    body = inspect.getsource(TS.find_best_premarket_pick)
    assert src_ts.count("SARO_PICK_ENGINE") == body.count("SARO_PICK_ENGINE") == 2, \
        "the dispatch seam lives ONLY inside find_best_premarket_pick (header comment + env read)"
    # the seam sits BEFORE the legacy body; the legacy funnel + momentum
    # code remains present and reachable (env='momentum')
    assert body.index("SARO_PICK_ENGINE") < body.index("SCANNER-V1")
    assert "find_best_pick_via_funnel" in body
    assert "_fetch_market_snapshot" in body
    # the scheduler is UNTOUCHED: both call sites still route through
    # find_best_premarket_pick and carry no engine logic of their own
    sched_path = os.path.join(_BACKEND, "app", "engines", "options",
                              "premarket_scheduler.py")
    with open(sched_path, encoding="utf-8") as fh:
        sched_src = fh.read()
    assert "SARO_PICK_ENGINE" not in sched_src
    assert sched_src.count("pick = await find_best_premarket_pick(db)") >= 2


# ═══ 11. coupled constants pinned (saro12 alert math + saro13 levels) ═════
def test_coupled_constants_pinned():
    assert C12.RVOL_MIN == 4.0
    assert C12.BB_PERIOD == 20 and C12.BB_STD == 2.0
    assert C12.KC_PERIOD == 20 and C12.KC_ATR_MULT == 1.5 and C12.KC_ATR_PERIOD == 20
    assert C12.MIN_BARS_FOR_SIGNAL == 40
    assert C13.TARGET_RANGE_MULT == 2.0 and C13.FALLBACK_STOP_PCT == 3.0
    assert LP.INTRADAY_TICKER_CAP == 10
    assert LP.STALE_MAX_DIFF_PCT == 3.0
    assert LP.ALERT_START_MIN == 9 * 60 + 33 and LP.ALERT_END_MIN == 12 * 60


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                fails += 1
                print(f"FAIL {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
