"""Tests for the Saro SUNDAY WATCHLIST (saro13/sunday_watchlist.py).

Covers: every compression-score component deterministic on synthetic data +
the exact weighting, top-5 ranking stability (input order can never change
the result), the recipient gate (= app.core.saro.SARO_RECIPIENT_WHERE, same
as the daily pick), the subject passing the EMAIL_KILL_SWITCH whitelist
(contains "Saro"), shadow rows correctly marked (shadow=true, watch_only,
source='sunwatch', matched_strategy='saro13:sunday_watchlist', entry=Friday
close), the no-entries-queued SAFETY invariants (clean-subprocess import
graph + source-token scan — ADAPTED from test_saro13: this module is the one
deliberate emailer, so app.services.email is allowed at SEND time but still
forbidden at IMPORT time, and raw mailers/pick/order paths stay forbidden),
and the latch/window/weekday gating including the Sunday-only inversion and
the window-end < 20:00 ET UTC-dateline pin.

Runs under pytest OR directly:  python tests/test_sunday_watchlist.py
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pandas as pd  # noqa: E402

from app.engines.scanner.saro13 import config as C  # noqa: E402
from app.engines.scanner.saro13 import indicators as I  # noqa: E402
import app.engines.scanner.saro13.sunday_watchlist as SW  # noqa: E402

_MODULE_PATH = os.path.join(_BACKEND, "app", "engines", "scanner", "saro13",
                            "sunday_watchlist.py")
_SRC = open(_MODULE_PATH, encoding="utf-8").read()

# Sunday of the synthetic test week (weekday()==6) and its Friday close date.
_SUNDAY = datetime(2026, 8, 23, 18, 5, 0)
_FRIDAY = date(2026, 8, 21)
_DATE_KEY = "2026-08-23"


# ── synthetic DAILY bar builders (same regimes as test_saro13) ─────────────
def _mkdf(closes, highs, lows, vols) -> pd.DataFrame:
    return pd.DataFrame({"open": list(map(float, closes)),
                         "high": list(map(float, highs)),
                         "low": list(map(float, lows)),
                         "close": list(map(float, closes)),
                         "volume": list(map(float, vols))})


def _compression_regime(n_wide: int = 60, n_tight: int = 30) -> pd.DataFrame:
    closes, highs, lows, vols = [], [], [], []
    for i in range(n_wide):
        c = 100.0 + 3.0 * math.sin(i * 0.7)
        closes.append(c)
        highs.append(c + 2.0)
        lows.append(c - 2.0)
        vols.append(2_000_000)
    for i in range(n_tight):
        amp = 0.5 * (1.0 - i / n_tight)
        c = 100.0 + (amp if i % 2 == 0 else -amp)
        rng = 2.0 - 1.7 * (i + 1) / n_tight
        closes.append(c)
        highs.append(c + rng)
        lows.append(c - rng)
        vols.append(2_000_000 - 1_200_000 * (i + 1) / n_tight)
    return _mkdf(closes, highs, lows, vols)


def _trending_regime(n: int = 90) -> pd.DataFrame:
    closes = [100.0 * (1.01 ** i) for i in range(n)]
    highs = [c * 1.012 for c in closes]
    lows = [c * 0.995 for c in closes]
    vols = [1_000_000 + 20_000 * i for i in range(n)]
    return _mkdf(closes, highs, lows, vols)


def _fmp_rows(df: pd.DataFrame, end: date = _FRIDAY) -> list[dict]:
    """DataFrame -> FMP-shaped raw EOD rows, newest-first, dated back from
    `end` (all < the Sunday date_key, so every bar survives the weekend
    live-partial drop — Friday EOD is complete)."""
    n = len(df)
    rows = []
    for i in range(n):
        d = end - timedelta(days=(n - 1 - i))
        r = df.iloc[i]
        rows.append({"date": d.strftime("%Y-%m-%d"), "open": float(r["open"]),
                     "high": float(r["high"]), "low": float(r["low"]),
                     "close": float(r["close"]), "volume": float(r["volume"])})
    rows.reverse()
    return rows


class _FakeRedis:
    """Dict-backed async redis stand-in (get/set/aclose only)."""
    def __init__(self, data=None, raise_on_set=False):
        self.d = dict(data or {})
        self.raise_on_set = raise_on_set

    async def get(self, k):
        return self.d.get(k)

    async def set(self, k, v, ex=None, nx=None):
        if self.raise_on_set:
            raise ConnectionError("redis down")
        if nx and k in self.d:
            return None
        self.d[k] = v
        return True

    async def aclose(self):
        pass


# ── scoring: each component deterministic on synthetic data ────────────────
def test_weights_sum_and_components():
    assert set(SW.WEIGHTS) == {"bb_width", "atr_streak", "rsi_neutral",
                               "vol_contraction", "float_tight"}
    assert abs(sum(SW.WEIGHTS.values()) - 1.0) < 1e-9
    assert all(w > 0 for w in SW.WEIGHTS.values())


def test_bb_width_rank_score():
    # decaying amplitude -> current width is the trailing-60 minimum ->
    # strict rank 0 -> full score
    n = 100
    dec = pd.Series([100.0 + (6.0 - 0.05 * i) * (1 if i % 2 == 0 else -1)
                     for i in range(n)])
    s, det = SW.bb_width_rank_score(dec)
    assert s == 1.0 and det["width_pct_rank"] == 0.0
    # growing amplitude -> current width is the trailing-60 maximum
    grow = pd.Series([100.0 + (0.1 + 0.05 * i) * (1 if i % 2 == 0 else -1)
                      for i in range(n)])
    s2, det2 = SW.bb_width_rank_score(grow)
    assert s2 < 0.05 and det2["width_pct_rank"] > 95.0
    # constant closes: every width 0 (all tied) -> strict rank 0 -> 1.0
    s3, _ = SW.bb_width_rank_score(pd.Series([10.0] * 100))
    assert s3 == 1.0
    # insufficient history -> 0.0 with a note, never a crash
    s4, det4 = SW.bb_width_rank_score(pd.Series([10.0] * 30))
    assert s4 == 0.0 and "insufficient" in str(det4.get("note"))


def test_atr_streak_score():
    assert SW.atr_streak_score(pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])) == (0.5, 5)
    assert SW.atr_streak_score(pd.Series([1.0, 2.0, 3.0])) == (0.1, 1)
    assert SW.atr_streak_score(pd.Series([2.0, 1.0])) == (0.2, 2)
    # 12 declining values -> raw streak 12, capped at 10 -> 1.0
    s, streak = SW.atr_streak_score(pd.Series([float(x) for x in range(12, 0, -1)]))
    assert s == 1.0 and streak == 12
    # break in the run: only the strictly-decreasing SUFFIX counts
    assert SW.atr_streak_score(pd.Series([3.0, 2.0, 5.0, 4.0, 3.0])) == (0.3, 3)
    # flat pair breaks the streak (strict), empty -> zero
    assert SW.atr_streak_score(pd.Series([3.0, 3.0])) == (0.1, 1)
    assert SW.atr_streak_score(pd.Series([], dtype=float)) == (0.0, 0)


def test_rsi_neutrality_score():
    assert SW.rsi_neutrality_score(50.0) == 1.0
    assert abs(SW.rsi_neutrality_score(45.0) - 0.8) < 1e-9
    assert abs(SW.rsi_neutrality_score(55.0) - 0.8) < 1e-9
    assert abs(SW.rsi_neutrality_score(62.5) - 0.5) < 1e-9
    assert SW.rsi_neutrality_score(25.0) == 0.0
    assert SW.rsi_neutrality_score(80.0) == 0.0
    assert SW.rsi_neutrality_score(None) == 0.0


def test_vol_contraction_score():
    assert SW.vol_contraction_score(1.0) == 0.0
    assert abs(SW.vol_contraction_score(0.9) - 0.2) < 1e-9
    assert abs(SW.vol_contraction_score(0.75) - 0.5) < 1e-9
    assert SW.vol_contraction_score(0.5) == 1.0
    assert SW.vol_contraction_score(0.3) == 1.0      # clamped
    assert SW.vol_contraction_score(1.2) == 0.0      # expansion
    assert SW.vol_contraction_score(None) == 0.0
    assert SW.vol_contraction_score(0.0) == 0.0      # degenerate


def test_float_tightness_score():
    s, shares = SW.float_tightness_score(5e9, 100.0)     # 50M shares proxy
    assert s == 1.0 and abs(shares - 5e7) < 1e-3
    s2, _ = SW.float_tightness_score(5e11, 100.0)        # 5B shares proxy
    assert s2 == 0.0
    s3, _ = SW.float_tightness_score(5e10, 100.0)        # 500M -> log midpoint
    assert abs(s3 - 0.5) < 1e-6
    assert SW.float_tightness_score(0, 100.0) == (0.0, None)
    assert SW.float_tightness_score(None, 100.0) == (0.0, None)
    assert SW.float_tightness_score(5e9, 0) == (0.0, None)


def test_compression_score_weighting():
    ones = {k: 1.0 for k in SW.WEIGHTS}
    assert SW.compression_score(ones) == 100.0
    assert SW.compression_score({k: 0.0 for k in SW.WEIGHTS}) == 0.0
    for k, wgt in SW.WEIGHTS.items():
        only = {k: 1.0}
        assert abs(SW.compression_score(only) - wgt * 100.0) < 1e-9
    mixed = {"bb_width": 1.0, "atr_streak": 0.5, "rsi_neutral": 0.8,
             "vol_contraction": 0.25, "float_tight": 0.0}
    expect = 100.0 * (0.30 * 1.0 + 0.20 * 0.5 + 0.15 * 0.8
                      + 0.20 * 0.25 + 0.15 * 0.0)
    assert abs(SW.compression_score(mixed) - expect) < 1e-9
    # out-of-range parts are clamped, junk keys ignored, missing keys = 0
    assert SW.compression_score({"bb_width": 7.0}) == 30.0
    assert SW.compression_score({"nonsense": 1.0}) == 0.0
    assert SW.compression_score({}) == 0.0


def test_score_candidate_coil_beats_trend():
    coil_df = _compression_regime()
    st = I.compression_state(coil_df)
    coil = SW.score_candidate(coil_df, st, 5e9)
    assert coil["parts"]["float_tight"] == 1.0            # ~50M share proxy
    assert coil["parts"]["atr_streak"] == 1.0             # long decline, capped
    assert coil["parts"]["bb_width"] >= 0.9
    assert coil["parts"]["rsi_neutral"] >= 0.5
    assert 0.3 <= coil["parts"]["vol_contraction"] <= 1.0
    assert coil["score"] >= 70.0
    trend_df = _trending_regime()
    st_t = I.compression_state(trend_df)
    trend = SW.score_candidate(trend_df, st_t, 5e9)
    assert trend["parts"]["rsi_neutral"] == 0.0           # RSI pinned hot
    assert trend["parts"]["vol_contraction"] == 0.0       # volume expanding
    assert trend["score"] < coil["score"]
    assert trend["score"] < 60.0


def test_criteria_hit_labels():
    st = I.compression_state(_compression_regime())
    hits = SW.criteria_hit(st)
    assert st["compressed"] is True
    assert len(hits) == 5                     # all strict criteria + no-break
    assert any("BB squeeze" in h for h in hits)
    assert any("ATR down" in h for h in hits)
    assert any("RSI neutral" in h for h in hits)
    assert any("volume contraction" in h for h in hits)
    assert any("not yet breaking" in h for h in hits)
    # a hot trend keeps only the labels it actually earns
    hits_t = SW.criteria_hit(I.compression_state(_trending_regime()))
    assert not any("RSI neutral" in h for h in hits_t)
    assert not any("volume contraction" in h for h in hits_t)
    assert SW.criteria_hit({}) == []


# ── ranking: top-5, deterministic under any input order ────────────────────
def test_rank_watchlist_stability():
    import random
    items = [{"ticker": "AAA", "score": 90.0}, {"ticker": "BBB", "score": 90.0},
             {"ticker": "CCC", "score": 85.0}, {"ticker": "DDD", "score": 80.0},
             {"ticker": "EEE", "score": 80.0}, {"ticker": "FFF", "score": 70.0}]
    expect = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    rnd = random.Random(42)
    for _ in range(10):
        shuffled = items[:]
        rnd.shuffle(shuffled)
        assert [c["ticker"] for c in SW.rank_watchlist(shuffled)] == expect
        top = SW.rank_watchlist(shuffled, top_n=5)
        assert [c["ticker"] for c in top] == expect[:5]
    assert SW.rank_watchlist([]) == []
    assert SW.TOP_N == 5


# ── the ranked funnel end-to-end on cached data (zero FMP calls) ───────────
def _seed_cache() -> dict:
    coil_rows = _fmp_rows(_compression_regime())
    trend_rows = _fmp_rows(_trending_regime())
    screener = [
        {"symbol": "COIL", "price": 100.2, "volume": 3_000_000,
         "marketCap": 5_000_000_000, "exchangeShortName": "NASDAQ",
         "isEtf": False, "isActivelyTrading": True},
        {"symbol": "TRND", "price": 242.0, "volume": 2_500_000,
         "marketCap": 8_000_000_000, "exchangeShortName": "NYSE",
         "isEtf": False, "isActivelyTrading": True},
        {"symbol": "MACO", "price": 100.2, "volume": 3_000_000,
         "marketCap": 5_000_000_000, "exchangeShortName": "NASDAQ",
         "isEtf": False, "isActivelyTrading": True},
        {"symbol": "PENY", "price": 2.0, "volume": 9_000_000,   # anti-junk fail
         "marketCap": 50_000_000, "exchangeShortName": "OTC"},
    ]
    d = {C.SCREEN_CACHE_FMT.format(date=_DATE_KEY): json.dumps(screener),
         C.DAILY_CACHE_FMT.format(date=_DATE_KEY, ticker="COIL"): json.dumps(coil_rows),
         C.DAILY_CACHE_FMT.format(date=_DATE_KEY, ticker="TRND"): json.dumps(trend_rows),
         C.DAILY_CACHE_FMT.format(date=_DATE_KEY, ticker="MACO"): json.dumps(coil_rows),
         C.MA_CACHE_FMT.format(date=_DATE_KEY): json.dumps(
             [{"targetedSymbol": "MACO"}]),
         C.NEWS_CACHE_FMT.format(date=_DATE_KEY, ticker="COIL"): json.dumps(
             [{"title": "COIL wins large defense contract"}]),
         C.NEWS_CACHE_FMT.format(date=_DATE_KEY, ticker="TRND"): json.dumps([])}
    return d


def test_build_watchlist_ranks_softly_and_hard_excludes_ma():
    r = _FakeRedis(_seed_cache())
    budget = {"eod_pulls": 0, "news_checks": 0, "cap_hit": False}
    final = asyncio.run(SW.build_watchlist(r, _DATE_KEY, budget))
    tickers = [c["ticker"] for c in final]
    assert "MACO" not in tickers          # M&A HARD exclude (fail-closed)
    assert "PENY" not in tickers          # anti-junk base screen
    # RANKS, not gates: the trending name makes a soft list of 2 even though
    # saro13's strict daily gates would reject it
    assert tickers == ["COIL", "TRND"]
    assert final[0]["score"] > final[1]["score"]
    assert final[0]["rank"] == 1 and final[1]["rank"] == 2
    assert final[0]["fully_coiled"] is True and final[1]["fully_coiled"] is False
    # entry anchor = Friday's completed close from the daily bars
    coil_df = _compression_regime()
    assert abs(final[0]["close"] - float(coil_df["close"].iloc[-1])) < 1e-6
    assert final[0]["catalyst"].startswith("COIL wins")
    assert len(final[0]["criteria"]) == 5
    # everything came from the Sunday caches: zero fresh FMP pulls
    assert budget["eod_pulls"] == 0 and budget["cap_hit"] is False


def test_build_watchlist_budget_cap_is_saro13s():
    # cache-less redis -> every history needs a REAL pull; the shared budget
    # dict must stop the funnel at C.DAILY_HISTORY_CAP=30 (the FMP fetch
    # itself fails fast in tests — no fmp key/network — which exercises the
    # counter without network)
    assert C.DAILY_HISTORY_CAP == 30
    src = _SRC
    assert "_fetch_daily_history" in src and "budget" in src
    assert "DAILY_HISTORY_CAP" in src     # cap logged when scoring stops


# ── delivery: subject, recipient gate, template honesty ────────────────────
def test_subject_passes_killswitch():
    from app.services.email import _killswitch_allows
    for n in (5, 3, 1):
        subject = SW.SUBJECT_FMT.format(n=n, plural=("" if n == 1 else "s"))
        assert "Saro" in subject
        assert _killswitch_allows(subject) is True
    # sanity: the predicate really does drop a non-whitelisted subject
    assert _killswitch_allows("Weekly watchlist — 5 coiled names") is False


def test_recipient_gate_is_saro_recipient_where():
    # the send path must use THE shared gate — same as the daily pick
    assert "SARO_RECIPIENT_WHERE" in _SRC and "ensure_saro_column" in _SRC
    assert re.search(r"from app\.core\.saro import ensure_saro_column,\s*"
                     r"SARO_RECIPIENT_WHERE", _SRC)
    # the ONLY users query is the gated recipient select
    froms = re.findall(r"FROM users\b[^\"']*", _SRC)
    assert len(froms) == 1 and "SARO_RECIPIENT_WHERE" in froms[0]
    # and the gate itself is the tier + is_active + opt-in fragment
    from app.core.saro import SARO_RECIPIENT_WHERE
    assert "u.is_active = true" in SARO_RECIPIENT_WHERE
    assert "saro_signals_enabled" in SARO_RECIPIENT_WHERE
    assert "tier_3" in SARO_RECIPIENT_WHERE


def test_email_template_is_watch_only():
    r = _FakeRedis(_seed_cache())
    budget = {"eod_pulls": 0, "news_checks": 0, "cap_hit": False}
    final = asyncio.run(SW.build_watchlist(r, _DATE_KEY, budget))
    html = SW.render_watchlist_html(_DATE_KEY, final)
    assert SW.NOT_A_SIGNAL_LINE in html
    assert "COIL" in html and "TRND" in html
    assert "coil score" in html and "Fri close" in html
    assert "defense contract" in html                  # catalyst note surfaced
    assert "Theta Algos LLC" in html                   # disclosure footer
    # forbidden trade-signal furniture: no order framing, no action links
    low = html.lower()
    for banned in ("/confirm", "/skip", "pending-trade", "pending_trade",
                   "token=", ">entry<", "stop:", "target:", "place order",
                   "buy ", "sell "):
        assert banned not in low, f"watch-only email contains {banned!r}"
    assert "href" not in low                           # zero links at all
    # and the sender is the killswitch-aware _send, lazily imported
    assert re.search(r"^\s+from app\.services\.email import _send$", _SRC,
                     re.MULTILINE), "_send must be lazily imported in-function"
    assert not re.search(r"^from app\.services\.email", _SRC, re.MULTILINE), \
        "app.services.email must never be imported at module top level"


# ── shadow rows: unmistakably watch-only, resolver-ready ───────────────────
def test_shadow_rows_correctly_marked():
    assert SW.MATCHED_STRATEGY == "saro13:sunday_watchlist"
    assert SW.MATCHED_STRATEGY.startswith(C.MATCHED_STRATEGY_PREFIX)
    assert SW.SOURCE_TAG == "sunwatch"
    assert SW.LATCH_FMT == "theta:sunwatch:{date}"
    # ONE insert, into email_signals_history only, hard-coded shadow=true +
    # watch_only, detection_ts stamped
    assert _SRC.count("INSERT INTO email_signals_history") == 1
    for m in re.finditer(r"INSERT INTO\s+([A-Za-z_.]+)", _SRC):
        assert m.group(1) == "email_signals_history"
    assert re.search(r"VALUES\s*\(NOW\(\),[^;]*true", _SRC), \
        "shadow=true not hard-coded in the INSERT"
    assert "'watch_only'" in _SRC and ":dts" in _SRC
    # the ensure chain (base cols + source/detection_ts) runs before inserts
    assert "_ensure_saro13_columns" in _SRC
    # entry = Friday close via the SAME level derivation as the daily cohort
    from app.engines.scanner.saro13.shadow import build_levels
    cand = {"ticker": "COIL", "close": 100.0, "lower_bb": 97.0,
            "swing_low": 96.5, "compression_range": 2.0}
    lv = build_levels(cand)
    assert lv["entry"] == 100.0                       # Friday close
    assert lv["stop"] == 97.0
    assert abs(lv["target"] - (100.0 + C.TARGET_RANGE_MULT * 2.0)) < 1e-9
    assert lv["stop"] < lv["entry"] < lv["target"]


# ── SAFETY: no entries queued, ever (adapted from test_saro12/13) ──────────
# ADAPTED token list: unlike the rest of the saro13 package this module DOES
# email — via app.services.email._send, lazily, killswitch-gated — so
# services.email is allowed at SEND time but still forbidden at IMPORT time.
# Raw mailers (smtplib/resend/sendgrid) and every pick/order path stay
# forbidden outright. Do NOT copy this relaxation back into test_saro13.
_FORBIDDEN_AT_IMPORT = ["app.engines.options.pick_router",
                        "app.engines.options.theta_scanner",
                        "app.services.email",   # lazy-only: never at import
                        "smtplib", "resend"]

_FORBIDDEN_IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+\S*(?:pick_router|theta_scanner|smtplib"
    r"|resend|sendgrid)", re.MULTILINE)
_FORBIDDEN_CALL_RE = re.compile(
    r"(?:emit_theta_pick|send_signal_email|send_pick_email"
    r"|send_consolidated_signals_email|place_order|submit_order"
    r"|create_order|paper_trade|queue_entry|_check_pending_stock_entries)\s*\(")


def test_safety_import_graph_no_entries_no_import_time_email():
    """Import the module in a CLEAN interpreter: no pick/order path may load,
    and app.services.email must NOT load at import time (send-time only)."""
    code = (
        "import sys\n"
        "import app.engines.scanner.saro13.sunday_watchlist\n"
        f"bad = [m for m in {_FORBIDDEN_AT_IMPORT!r} if m in sys.modules]\n"
        "assert not bad, f'forbidden modules imported: {bad}'\n"
        "print('IMPORT-GRAPH-CLEAN')\n"
    )
    res = subprocess.run([sys.executable, "-c", code], cwd=_BACKEND,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert "IMPORT-GRAPH-CLEAN" in res.stdout


def test_safety_no_forbidden_tokens_in_source():
    m = _FORBIDDEN_IMPORT_RE.search(_SRC)
    assert m is None, f"forbidden import: {m.group(0)!r}"
    m = _FORBIDDEN_CALL_RE.search(_SRC)
    assert m is None, f"forbidden call site: {m.group(0)!r}"
    # the GC-proof spawn rule: NEVER a naked asyncio.create_task CALL — every
    # spawn goes through shadow._spawn_retained (2026-08-07 incident); prose
    # mentions in comments are fine, call sites are not
    assert "asyncio.create_task(" not in _SRC
    assert "create_task(" not in _SRC.replace("asyncio.create_task(", "")
    assert "_spawn_retained(" in _SRC
    # no paper/live entry tables anywhere near this module
    for banned in ("pending_stock_entries", "INSERT INTO trades",
                   "INSERT INTO paper"):
        assert banned not in _SRC, f"entry-path token {banned!r} in source"


# ── gating: Sunday-only window, latch, env gate, dateline pin ──────────────
def test_window_pinned_before_utc_dateline():
    assert SW.SPAWN_START_S == 18 * 3600
    assert SW.SPAWN_END_S == 19 * 3600 + 30 * 60
    # HARD PIN: window end strictly before 20:00 ET. Past 20:00 ET, NOW() in
    # the UTC DB stamps tomorrow's date — rows read future-dated from ET and
    # escape the ET-day dedup (the 2026-08-17 anomaly mechanism).
    assert SW.SPAWN_END_S < 20 * 3600


def test_spawn_check_gates_weekday_window_env():
    sat = datetime(2026, 8, 22, 18, 30, 0)       # Saturday, in-window time
    assert sat.weekday() == 5
    assert SW.maybe_spawn_sunday_watchlist(_now_et_fn=lambda: sat) is False
    mon = datetime(2026, 8, 24, 18, 30, 0)       # Monday evening
    assert SW.maybe_spawn_sunday_watchlist(_now_et_fn=lambda: mon) is False
    early = datetime(2026, 8, 23, 17, 59, 59)    # Sunday, 1s before window
    assert SW.maybe_spawn_sunday_watchlist(_now_et_fn=lambda: early) is False
    late = datetime(2026, 8, 23, 19, 30, 0)      # Sunday, window end (excl.)
    assert SW.maybe_spawn_sunday_watchlist(_now_et_fn=lambda: late) is False
    os.environ[SW.ENV_GATE] = "0"                # env gate blocks in-window
    try:
        inwin = datetime(2026, 8, 23, 18, 30, 0)
        assert SW.maybe_spawn_sunday_watchlist(_now_et_fn=lambda: inwin) is False
    finally:
        os.environ.pop(SW.ENV_GATE, None)


def test_spawn_fires_sunday_in_window_once():
    sun = datetime(2026, 8, 23, 18, 0, 0)        # Sunday 18:00:00 sharp
    assert sun.weekday() == 6

    async def go():
        called = {"n": 0}

        async def fake_run(**kw):
            called["n"] += 1
            return {"status": "test"}

        real = SW.run_sunday_watchlist
        SW.run_sunday_watchlist = fake_run
        try:
            first = SW.maybe_spawn_sunday_watchlist(_now_et_fn=lambda: sun)
            second = SW.maybe_spawn_sunday_watchlist(_now_et_fn=lambda: sun)
            await asyncio.sleep(0.05)            # let the retained task run
        finally:
            SW.run_sunday_watchlist = real
            SW._spawned_dates.discard("2026-08-23")
        return first, second, called["n"]

    first, second, n = asyncio.run(go())
    assert first is True                          # spawned
    assert second is False                        # in-process date dedup
    assert n == 1                                 # the task actually ran once


def test_run_gates_not_sunday_env_latch_redis():
    fake = _FakeRedis()
    mon = datetime(2026, 8, 24, 18, 10, 0)
    res = asyncio.run(SW.run_sunday_watchlist(redis_client=fake,
                                              _now_et_fn=lambda: mon))
    assert res == {"status": "not-sunday"} and fake.d == {}
    os.environ[SW.ENV_GATE] = "0"
    try:
        res = asyncio.run(SW.run_sunday_watchlist(redis_client=fake,
                                                  _now_et_fn=lambda: _SUNDAY))
        assert res == {"status": "disabled"}
    finally:
        os.environ.pop(SW.ENV_GATE, None)
    # latch already taken -> latched, nothing else happens
    taken = _FakeRedis({SW.LATCH_FMT.format(date=_DATE_KEY): "running"})
    res = asyncio.run(SW.run_sunday_watchlist(redis_client=taken,
                                              _now_et_fn=lambda: _SUNDAY))
    assert res == {"status": "latched"}
    # redis down -> skip loudly, no dup risk, never raises
    down = _FakeRedis(raise_on_set=True)
    res = asyncio.run(SW.run_sunday_watchlist(redis_client=down,
                                              _now_et_fn=lambda: _SUNDAY))
    assert res == {"status": "no-redis"}


# ── direct-invoke fallback (pytest may be absent in the image) ─────────────
def main() -> int:
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {name}: {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
