"""Tests for the Saro 1.3 SHADOW scanner (app/engines/scanner/saro13/).

Covers: the pinned indicator definitions on synthetic DAILY bars (BB width
formula + lowest-15%-of-60d percentile gate, ATR(14) strictly-declining-5,
RSI(14) [45,55] band with the >65 hard reject, volume < SMA20 contraction,
the +3% daily-change reject, live-partial exclusion in daily_rows_to_df),
the M&A hard-exclude polarity (fail CLOSED on a hit, fail OPEN on outage),
IV-rank math + the pinned monthly-tenor rule + the mid_iv->smv_vol
fallback, level derivation (entry=last close, stop=lowerBB/swing-low/-3%,
target=entry+2x compression range — never null), and the SAFETY tests: the
saro13 import graph must never pull pick_router / emit_theta_pick /
app.services.email / smtplib / order paths (clean subprocess + source-token
scan), rows are unmistakably shadow, the S4 alert stub is OFF by default,
and the spawn check self-gates (weekend/window/env).

Runs under pytest OR directly:  python tests/test_saro13.py
"""
from __future__ import annotations

import asyncio
import math
import os
import re
import subprocess
import sys
from datetime import date, datetime

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pandas as pd  # noqa: E402

from app.engines.scanner.saro13 import config as C  # noqa: E402
from app.engines.scanner.saro13 import indicators as I  # noqa: E402
from app.engines.scanner.saro13.screen import (  # noqa: E402
    ma_target_hit, news_keyword_hit, normalize_exchange, passes_base_screen)
from app.engines.scanner.saro13.iv_snapshot import (  # noqa: E402
    atm_iv_from_chain, compute_iv_rank, is_monthly_expiration,
    pick_monthly_expiration, third_friday)
from app.engines.scanner.saro13.shadow import build_levels  # noqa: E402


# ── synthetic DAILY bar builders ───────────────────────────────────────────
def _mkdf(closes, highs, lows, vols) -> pd.DataFrame:
    return pd.DataFrame({"open": list(map(float, closes)),
                         "high": list(map(float, highs)),
                         "low": list(map(float, lows)),
                         "close": list(map(float, closes)),
                         "volume": list(map(float, vols))})


def _compression_regime(n_wide: int = 60, n_tight: int = 30) -> pd.DataFrame:
    """Wide, choppy phase then a tightening coil: shrinking ranges (ATR
    strictly down), converging closes (BB width -> 60d low, RSI ~50),
    declining volume (below SMA20). The textbook S1+S2 pass."""
    closes, highs, lows, vols = [], [], [], []
    for i in range(n_wide):
        c = 100.0 + 3.0 * math.sin(i * 0.7)
        closes.append(c)
        highs.append(c + 2.0)
        lows.append(c - 2.0)
        vols.append(2_000_000)
    for i in range(n_tight):
        amp = 0.5 * (1.0 - i / n_tight)          # decaying oscillation
        c = 100.0 + (amp if i % 2 == 0 else -amp)
        rng = 2.0 - 1.7 * (i + 1) / n_tight      # 1.94 -> 0.30 strictly down
        closes.append(c)
        highs.append(c + rng)
        lows.append(c - rng)
        vols.append(2_000_000 - 1_200_000 * (i + 1) / n_tight)  # 2.0M -> 0.8M
    return _mkdf(closes, highs, lows, vols)


def _trending_regime(n: int = 90) -> pd.DataFrame:
    """Strong uptrend with expanding ranges and rising volume — fails the
    coil on multiple legs (RSI hot, ATR rising, width expanding, vol up)."""
    closes = [100.0 * (1.01 ** i) for i in range(n)]
    highs = [c * 1.012 for c in closes]
    lows = [c * 0.995 for c in closes]
    vols = [1_000_000 + 20_000 * i for i in range(n)]
    return _mkdf(closes, highs, lows, vols)


# ── daily_rows_to_df: live-partial exclusion + ordering + wrappers ─────────
def test_daily_rows_to_df_drops_live_partial_and_sorts():
    rows = [  # newest-first, today included (the probed FMP shape)
        {"date": "2026-08-07", "open": 1, "high": 1, "low": 1, "close": 101.0, "volume": 10},
        {"date": "2026-08-06", "open": 1, "high": 1, "low": 1, "close": 100.5, "volume": 20},
        {"date": "2026-08-05", "open": 1, "high": 1, "low": 1, "close": 99.0, "volume": 30},
    ]
    df = I.daily_rows_to_df(rows, today_et="2026-08-07")
    assert list(df["close"]) == [99.0, 100.5]      # today's LIVE PARTIAL dropped
    # shuffled upstream ordering must not matter (explicit sort)
    df2 = I.daily_rows_to_df([rows[2], rows[0], rows[1]], today_et="2026-08-07")
    assert list(df2["close"]) == [99.0, 100.5]
    # wrapped-dict variant tolerated (known FMP shape-flip failure mode)
    df3 = I.daily_rows_to_df({"historical": rows}, today_et="2026-08-07")
    assert list(df3["close"]) == [99.0, 100.5]
    assert I.daily_rows_to_df([], "2026-08-07") is None
    assert I.daily_rows_to_df(None, "2026-08-07") is None


# ── BB width: pinned formula + lowest-15% percentile gate ──────────────────
def test_bb_width_formula():
    # alternating 9/11: mean 10, population std 1 -> width = 4*1/10 = 0.4
    closes = pd.Series([9.0, 11.0] * 20)
    w = I.bb_width(closes).dropna()
    assert abs(float(w.iloc[-1]) - 0.4) < 1e-9
    # constant closes -> zero width
    w0 = I.bb_width(pd.Series([10.0] * 40)).dropna()
    assert abs(float(w0.iloc[-1])) < 1e-12


def test_bbwidth_lowest_pct_gate():
    df = _compression_regime()
    ok, det = I.bbwidth_in_lowest_pct(I.bb_width(df["close"]))
    assert ok is True and det["bb_width"] <= det["pct_threshold"]
    df_t = _trending_regime()
    ok_t, _ = I.bbwidth_in_lowest_pct(I.bb_width(df_t["close"]))
    assert ok_t is False
    # insufficient history -> False with a note, never a crash
    ok_s, det_s = I.bbwidth_in_lowest_pct(I.bb_width(pd.Series([10.0] * 30)))
    assert ok_s is False and "insufficient" in str(det_s.get("note"))


# ── ATR: strictly-declining-5 pin ──────────────────────────────────────────
def test_atr_declining_pin():
    assert I.atr_declining(pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])) is True
    assert I.atr_declining(pd.Series([5.0, 4.0, 3.0, 2.0, 2.0])) is False  # flat pair
    assert I.atr_declining(pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])) is False
    assert I.atr_declining(pd.Series([3.0, 2.0, 1.0])) is False            # < 5 values
    df = _compression_regime()
    assert I.atr_declining(I.atr(df["high"], df["low"], df["close"])) is True
    df_t = _trending_regime()
    assert I.atr_declining(I.atr(df_t["high"], df_t["low"], df_t["close"])) is False


# ── RSI band + volume contraction + daily change ───────────────────────────
def test_rsi_band_and_hard_reject():
    df = _compression_regime()
    r = I.rsi(df["close"]).dropna()
    assert C.RSI_BAND_LO <= float(r.iloc[-1]) <= C.RSI_BAND_HI
    r_hot = I.rsi(_trending_regime()["close"]).dropna()
    assert float(r_hot.iloc[-1]) > C.RSI_HARD_REJECT  # uptrend -> hard-reject zone


def test_vol_contraction_pin():
    df = _compression_regime()
    ok, det = I.vol_contraction(df["volume"])
    assert ok is True and det["vol"] < det["vol_sma"] and det["vol_ratio"] < 1.0
    ok_t, _ = I.vol_contraction(_trending_regime()["volume"])
    assert ok_t is False
    ok_s, det_s = I.vol_contraction(pd.Series([1.0] * 5))
    assert ok_s is False and "insufficient" in str(det_s.get("note"))


def test_passes_daily_change():
    ok, pct = I.passes_daily_change(104.0, 100.0)      # +4% -> reject
    assert ok is False and abs(pct - 4.0) < 1e-9
    assert I.passes_daily_change(102.9, 100.0)[0] is True   # +2.9% ok
    assert I.passes_daily_change(95.0, 100.0)[0] is True    # red day is NOT a breakout
    assert I.passes_daily_change(None, 100.0) == (True, None)   # unknown -> pass
    assert I.passes_daily_change(100.0, None) == (True, None)


# ── composed S1+S2 state ───────────────────────────────────────────────────
def test_compression_state_passes_textbook_coil():
    st = I.compression_state(_compression_regime())
    assert st["compressed"] is True, st["reject_reasons"]
    assert st["bb_width_ok"] and st["atr_declining_ok"] and st["rsi_ok"] and st["vol_ok"]
    assert st["close"] is not None and st["lower_bb"] is not None
    assert st["compression_range"] > 0 and st["swing_low"] is not None
    assert st["reject_reasons"] == []


def test_compression_state_rejects_trend():
    st = I.compression_state(_trending_regime())
    assert st["compressed"] is False and len(st["reject_reasons"]) >= 2
    assert st["rsi_hard_reject"] is True  # spec's explicit >65 hard reject


def test_compression_state_single_leg_blocks():
    # volume spike on the last session -> only the contraction leg fails
    df = _compression_regime()
    df.loc[df.index[-1], "volume"] = 5_000_000
    st = I.compression_state(df)
    assert st["compressed"] is False
    assert any("SMA" in r for r in st["reject_reasons"])
    # last completed session pops +5% -> daily-change leg rejects
    df2 = _compression_regime()
    df2.loc[df2.index[-1], "close"] = float(df2["close"].iloc[-2]) * 1.05
    df2.loc[df2.index[-1], "high"] = float(df2["close"].iloc[-1]) + 0.3
    st2 = I.compression_state(df2)
    assert st2["compressed"] is False
    assert any("daily change" in r for r in st2["reject_reasons"])


def test_compression_state_safe_on_bad_input():
    assert I.compression_state(None)["compressed"] is False
    st = I.compression_state(_compression_regime().head(30))  # < MIN_DAILY_SESSIONS
    assert st["compressed"] is False
    assert any("insufficient" in r for r in st["reject_reasons"])


# ── screen: base filter + M&A polarity ─────────────────────────────────────
def _good_row(**over):
    row = {"symbol": "ABCD", "price": 50.0, "volume": 3_000_000,
           "marketCap": 5_000_000_000, "exchangeShortName": "NASDAQ",
           "isEtf": False, "isActivelyTrading": True}
    row.update(over)
    return row


def test_base_screen_accepts_and_rejects():
    ok, why = passes_base_screen(_good_row())
    assert ok is True, why
    assert passes_base_screen(_good_row(price=5.0))[0] is False       # < $10
    assert passes_base_screen(_good_row(price=900.0))[0] is False     # > $500
    assert passes_base_screen(_good_row(volume=500_000))[0] is False  # < 1M
    assert passes_base_screen(_good_row(marketCap=500_000_000))[0] is False
    assert passes_base_screen(_good_row(exchangeShortName="OTC"))[0] is False
    assert passes_base_screen(_good_row(isEtf=True))[0] is False
    assert passes_base_screen(_good_row(isActivelyTrading=False))[0] is False
    assert normalize_exchange({"exchange": "NasdaqGS"}) == "NASDAQ"
    assert normalize_exchange({"exchange": "New York Stock Exchange"}) == "NYSE"
    assert normalize_exchange({"exchangeShortName": "OTC"}) == ""


def test_ma_hard_exclude_polarity():
    # confirmed targetedSymbol hit -> fail CLOSED (exclude)
    assert ma_target_hit([{"targetedSymbol": "ABCD"}], "ABCD") is True
    assert ma_target_hit([{"targetedSymbol": "abcd"}], "ABCD") is True  # case-insens.
    assert ma_target_hit([{"targetedSymbol": "OTHER"}], "ABCD") is False
    # unavailable/empty data is NOT a hit (the fail-open leg lives in ma_gate)
    assert ma_target_hit(None, "ABCD") is False
    assert ma_target_hit([], "ABCD") is False


def test_ma_headline_keywords():
    assert news_keyword_hit([{"title": "Acme agrees to $2B buyout"}]) is not None
    assert news_keyword_hit([{"title": "Merger talks heat up for Acme"}]) is not None
    assert news_keyword_hit([{"title": "Acme explores take-private deal"}]) is not None
    assert news_keyword_hit([{"title": "Acquisition rumors swirl"}]) is not None
    assert news_keyword_hit([{"title": "Acme beats quarterly earnings"}]) is None
    assert news_keyword_hit([]) is None and news_keyword_hit(None) is None


# ── IV: rank math, tenor rule, mid_iv -> smv_vol fallback ──────────────────
def test_compute_iv_rank_math():
    rank = compute_iv_rank([0.2, 0.3, 0.6, 0.5])   # current 0.5 in [0.2, 0.6]
    assert rank is not None and abs(rank - 75.0) < 1e-9
    assert abs(compute_iv_rank([0.6, 0.2]) - 0.0) < 1e-9      # current == min
    assert compute_iv_rank([0.4, 0.4, 0.4]) is None           # degenerate
    assert compute_iv_rank([]) is None and compute_iv_rank([0.5]) is None


def test_monthly_tenor_rule():
    assert third_friday(2026, 8) == date(2026, 8, 21)
    assert third_friday(2026, 9) == date(2026, 9, 18)
    assert is_monthly_expiration("2026-08-21") is True
    assert is_monthly_expiration("2026-08-07") is False   # a Friday, not the 3rd
    exps = ["2026-08-07", "2026-08-10", "2026-08-21", "2026-09-18"]
    assert pick_monthly_expiration(exps, date(2026, 8, 7)) == "2026-08-21"
    # monthly under min DTE -> roll to the NEXT monthly
    assert pick_monthly_expiration(exps, date(2026, 8, 17)) == "2026-09-18"
    # no monthlies listed -> nearest expiration >= 7 DTE
    assert pick_monthly_expiration(["2026-08-10", "2026-08-12", "2026-08-17"],
                                   date(2026, 8, 7)) == "2026-08-17"
    # nothing far enough out -> last resort furthest listed
    assert pick_monthly_expiration(["2026-08-10"], date(2026, 8, 7)) == "2026-08-10"
    assert pick_monthly_expiration([], date(2026, 8, 7)) is None


def test_atm_iv_mid_then_smv_fallback():
    chain = [
        {"strike": 95.0, "option_type": "call", "greeks": {"mid_iv": 0.0, "smv_vol": 0.0}},
        {"strike": 100.0, "option_type": "call", "greeks": {"mid_iv": 0.52, "smv_vol": 0.5}},
        {"strike": 100.0, "option_type": "put", "greeks": {"mid_iv": 0.48, "smv_vol": 0.5}},
        {"strike": 105.0, "option_type": "call", "greeks": {"mid_iv": 0.6}},
    ]
    res = atm_iv_from_chain(chain, last=101.0)
    assert res["strike"] == 100.0 and abs(res["iv"] - 0.50) < 1e-9
    assert res["source"] == "mid_iv"
    # probed live nuance: mid_iv 0.0 on an illiquid row while smv_vol stays
    # populated -> the snapshot must fall back to smv_vol
    res2 = atm_iv_from_chain(
        [{"strike": 100.0, "greeks": {"mid_iv": 0.0, "smv_vol": 0.468}}], 100.0)
    assert abs(res2["iv"] - 0.468) < 1e-9 and res2["source"] == "smv_vol"
    # dead ATM strike (all zeros) walks outward to the next strike
    res3 = atm_iv_from_chain([{"strike": 100.0, "greeks": {"mid_iv": 0, "smv_vol": 0}},
                              {"strike": 105.0, "greeks": {"mid_iv": 0.4}}], 100.0)
    assert res3["strike"] == 105.0
    assert atm_iv_from_chain([], 100.0) is None
    assert atm_iv_from_chain(chain, None) is None


# ── levels: entry/stop/target always present (NOT NULL columns) ────────────
def _cand(**over):
    c = {"ticker": "ABCD", "price": 100.6, "close": 100.0, "lower_bb": 97.0,
         "swing_low": 96.5, "compression_range": 2.0}
    c.update(over)
    return c


def test_build_levels_spec_derivation():
    lv = build_levels(_cand())
    assert lv["entry"] == 100.0                 # entry = last COMPLETED close
    assert lv["stop"] == 97.0                   # lower BB first
    assert abs(lv["target"] - (100.0 + C.TARGET_RANGE_MULT * 2.0)) < 1e-9
    assert lv["stop"] < lv["entry"] < lv["target"]
    # lower BB unusable -> swing low
    assert build_levels(_cand(lower_bb=None))["stop"] == 96.5
    assert build_levels(_cand(lower_bb=101.0))["stop"] == 96.5  # above entry: skip


def test_build_levels_never_null():
    lv = build_levels(_cand(lower_bb=None, swing_low=None))     # -3% last resort
    assert all(lv[k] is not None and lv[k] > 0 for k in ("entry", "stop", "target"))
    assert abs(lv["stop"] - 97.0) < 1e-9
    assert lv["stop"] < lv["entry"] < lv["target"]
    lv2 = build_levels(_cand(compression_range=0.0))            # degenerate range
    assert lv2["stop"] < lv2["entry"] < lv2["target"]
    lv3 = build_levels({"ticker": "X"})                         # worst case input
    assert lv3["stop"] < lv3["entry"] < lv3["target"] and lv3["stop"] > 0


# ── config identity + budget ceiling ───────────────────────────────────────
def test_config_identity_and_budget():
    assert C.SOURCE_TAG == "saro13_shadow"
    assert C.MATCHED_STRATEGY == "saro13:compression_coil"
    assert C.MATCHED_STRATEGY.startswith(C.MATCHED_STRATEGY_PREFIX)
    assert "saro13" in C.LATCH_SCAN_FMT and "saro13" in C.LATCH_IV_FMT
    for fmt in (C.SCREEN_CACHE_FMT, C.DAILY_CACHE_FMT, C.MA_CACHE_FMT,
                C.NEWS_CACHE_FMT, C.CANDIDATES_KEY_FMT):
        assert fmt.startswith("saro13:")        # never another cohort's keys
    worst = 1 + C.DAILY_HISTORY_CAP + 1 + C.NEWS_CHECK_CAP
    assert worst <= 45, f"FMP worst case {worst} breaches the 45/day ceiling"


# ── SAFETY: shadow-only import graph + no forbidden call sites ─────────────
_FORBIDDEN_MODULES = ["app.engines.options.pick_router",
                      "app.engines.options.theta_scanner",
                      "app.services.email", "smtplib"]

_FORBIDDEN_IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+\S*(?:pick_router|theta_scanner|services\.email"
    r"|smtplib|resend|sendgrid)", re.MULTILINE)
_FORBIDDEN_CALL_RE = re.compile(
    r"(?:emit_theta_pick|send_email|send_signal_email|send_pick_email"
    r"|place_order|submit_order|create_order|paper_trade|queue_entry)\s*\(")

_PKG = os.path.join(_BACKEND, "app", "engines", "scanner", "saro13")

# The ORIGINAL shadow-only saro13 modules. sunday_watchlist.py (2026-08-18)
# joined the package as the ONE deliberate emailing exception (Sunday
# WATCHLIST cohort: killswitch-whitelisted subject, SARO-activated recipients,
# still ZERO entries) — it carries its own INSERT and an ADAPTED
# forbidden-token list in tests/test_sunday_watchlist.py. The invariants
# below stay byte-identical for these core files; do NOT fold
# sunday_watchlist into this list, and do NOT copy its email allowance here.
_CORE_FILES = ["__init__.py", "config.py", "indicators.py", "screen.py",
               "iv_snapshot.py", "shadow.py"]


def test_safety_import_graph_clean_subprocess():
    """Import every saro13 module in a CLEAN interpreter and assert none of
    the email/pick/broker-order modules got imported. Subprocess so pytest
    conftest imports can't mask a violation."""
    code = (
        "import sys\n"
        "import app.engines.scanner.saro13.config\n"
        "import app.engines.scanner.saro13.indicators\n"
        "import app.engines.scanner.saro13.screen\n"
        "import app.engines.scanner.saro13.iv_snapshot\n"
        "import app.engines.scanner.saro13.shadow\n"
        f"bad = [m for m in {_FORBIDDEN_MODULES!r} if m in sys.modules]\n"
        "assert not bad, f'forbidden modules imported: {bad}'\n"
        "print('IMPORT-GRAPH-CLEAN')\n"
    )
    res = subprocess.run([sys.executable, "-c", code], cwd=_BACKEND,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert "IMPORT-GRAPH-CLEAN" in res.stdout


def test_safety_no_forbidden_tokens_in_source():
    """No saro13 source file may IMPORT an email/pick module or CALL an
    email/pick/order function (docstring mentions are fine — this scans
    import statements and call sites)."""
    for fn in _CORE_FILES:
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(_PKG, fn), encoding="utf-8").read()
        m = _FORBIDDEN_IMPORT_RE.search(src)
        assert m is None, f"{fn}: forbidden import: {m.group(0)!r}"
        m = _FORBIDDEN_CALL_RE.search(src)
        assert m is None, f"{fn}: forbidden call site: {m.group(0)!r}"


def test_safety_rows_are_unmistakably_shadow():
    """The email_signals_history INSERT must hard-code shadow=true,
    watch_only, the saro13 source tag and strategy prefix, and stamp
    detection_ts. The only other INSERT in the package is the saro_iv_history
    accumulator."""
    src = open(os.path.join(_PKG, "shadow.py"), encoding="utf-8").read()
    assert re.search(r"INSERT INTO email_signals_history", src)
    assert re.search(r"VALUES\s*\(NOW\(\),[^;]*true", src), "shadow=true not hard-coded"
    assert "'watch_only'" in src
    assert "detection_ts" in src and "detection_ts timestamptz" in src  # S4 stamp
    assert C.SOURCE_TAG == "saro13_shadow"
    esh_inserts = 0
    for fn in _CORE_FILES:
        if not fn.endswith(".py"):
            continue
        s = open(os.path.join(_PKG, fn), encoding="utf-8").read()
        esh_inserts += s.count("INSERT INTO email_signals_history")
        for m in re.finditer(r"INSERT INTO\s+\{?([A-Za-z_.]+)\}?", s):
            tgt = m.group(1)
            assert tgt in ("email_signals_history", "C.IV_TABLE"), \
                f"{fn}: unexpected INSERT target {tgt!r}"
    assert esh_inserts == 1
    assert C.IV_TABLE == "saro_iv_history"


def test_s4_alert_stub_off_by_default():
    """SARO13_ALERT_WEBHOOK unset/empty -> the stub is a no-op ('off'), no
    network, no exceptions. Shadow honesty: a shadow track never alerts."""
    from app.engines.scanner.saro13.shadow import _fire_instant_alert
    old = os.environ.pop(C.ALERT_WEBHOOK_ENV, None)
    try:
        assert asyncio.run(_fire_instant_alert({"ticker": "ABCD"})) == "off"
        os.environ[C.ALERT_WEBHOOK_ENV] = "   "
        assert asyncio.run(_fire_instant_alert({"ticker": "ABCD"})) == "off"
    finally:
        os.environ.pop(C.ALERT_WEBHOOK_ENV, None)
        if old is not None:
            os.environ[C.ALERT_WEBHOOK_ENV] = old


def test_spawn_check_gates_weekend_and_window():
    """maybe_spawn_saro13_shadow must self-gate (the fast loop runs 24/7)."""
    from app.engines.scanner.saro13.shadow import maybe_spawn_saro13_shadow
    sat = datetime(2026, 8, 8, 10, 0, 0)      # Saturday
    assert maybe_spawn_saro13_shadow(_now_et_fn=lambda: sat) is False
    pre = datetime(2026, 8, 10, 9, 0, 0)      # Monday, before 09:33
    assert maybe_spawn_saro13_shadow(_now_et_fn=lambda: pre) is False
    lunch = datetime(2026, 8, 10, 12, 0, 0)   # 11:00-18:30 dead zone
    assert maybe_spawn_saro13_shadow(_now_et_fn=lambda: lunch) is False
    late = datetime(2026, 8, 10, 21, 30, 0)   # after the IV window
    assert maybe_spawn_saro13_shadow(_now_et_fn=lambda: late) is False
    os.environ[C.ENV_GATE] = "0"              # env gate blocks even in-window
    try:
        inwin = datetime(2026, 8, 10, 9, 40, 0)
        assert maybe_spawn_saro13_shadow(_now_et_fn=lambda: inwin) is False
    finally:
        os.environ[C.ENV_GATE] = "1"


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
