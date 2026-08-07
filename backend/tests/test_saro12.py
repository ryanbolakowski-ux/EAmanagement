"""Tests for the Saro 1.2 SHADOW scanner (app/engines/scanner/saro12/).

Covers: indicator math on synthetic bars (RSI crossing, MACD histogram
sign, BB-inside-KC squeeze on/off, upper-KC breakout alert, RVOL), the
Section-1 screen filters (each anti-junk rule accepts AND rejects, incl.
the mandated fail-open behavior), OI-surge math, level fallbacks, and the
SAFETY test: the saro12 import graph must never pull pick_router /
emit_theta_pick / app.services.email / smtplib / order paths (checked in a
clean subprocess so pytest conftest imports can't mask it, plus a source
scan for forbidden import/call sites).

Runs under pytest OR directly:  python tests/test_saro12.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import pandas as pd  # noqa: E402

from app.engines.scanner.saro12 import config as C  # noqa: E402
from app.engines.scanner.saro12 import indicators as I  # noqa: E402
from app.engines.scanner.saro12.screen import (  # noqa: E402
    normalize_exchange, passes_anti_junk, passes_base_screen)
from app.engines.scanner.saro12.oi_snapshot import (  # noqa: E402
    compute_oi_surge, is_oi_surge)
from app.engines.scanner.saro12.shadow import build_levels  # noqa: E402


# ── synthetic bar builders ─────────────────────────────────────────────────
def _df(closes, highs=None, lows=None, vols=None) -> pd.DataFrame:
    closes = list(map(float, closes))
    highs = list(map(float, highs)) if highs is not None else [c for c in closes]
    lows = list(map(float, lows)) if lows is not None else [c for c in closes]
    vols = list(map(float, vols)) if vols is not None else [10_000.0] * len(closes)
    return pd.DataFrame({"open": closes, "high": highs, "low": lows,
                         "close": closes, "volume": vols})


def _squeeze_regime(n=60, close=10.0, half_range=0.4) -> pd.DataFrame:
    """Constant closes with real high-low range: std(close)=0 << 1.5*ATR
    -> BB fully inside KC -> squeeze ON."""
    return _df([close] * n, highs=[close + half_range] * n,
               lows=[close - half_range] * n)


def _trending_regime(n=60, start=10.0, step=0.3) -> pd.DataFrame:
    """Strong linear trend with hairline bar ranges: std(close) large vs
    ATR -> BB outside KC -> squeeze OFF."""
    closes = [start + step * i for i in range(n)]
    return _df(closes, highs=[c + 0.01 for c in closes],
               lows=[c - 0.01 for c in closes])


# ── indicators: RSI ────────────────────────────────────────────────────────
def test_rsi_bounds_and_direction():
    dn_up = [50 - 0.2 * i for i in range(20)] + [46 + 0.4 * i for i in range(1, 21)]
    r = I.rsi(pd.Series(dn_up)).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    assert float(r.iloc[-1]) > 65.0          # strong rise -> high RSI
    r_dn = I.rsi(pd.Series([50 - 0.3 * i for i in range(40)])).dropna()
    assert float(r_dn.iloc[-1]) < 35.0       # steady decline -> low RSI


def test_rsi_cross_above_detects_recent_cross():
    dn = [50 - 0.2 * i for i in range(20)]
    up = [dn[-1] + 0.4 * i for i in range(1, 21)]
    r = I.rsi(pd.Series(dn + up))
    assert I.rsi_cross_above(r, level=65.0, lookback=15) is True
    # falling series never crosses up
    r_dn = I.rsi(pd.Series([50 - 0.3 * i for i in range(40)]))
    assert I.rsi_cross_above(r_dn, level=65.0, lookback=15) is False
    # crossed once but fell back below the level -> not a live cross
    fade = dn + up + [up[-1] - 1.5 * i for i in range(1, 16)]
    r_fade = I.rsi(pd.Series(fade))
    assert I.rsi_cross_above(r_fade, level=65.0, lookback=15) is False


# ── indicators: MACD histogram ─────────────────────────────────────────────
def test_macd_histogram_sign():
    # flat base then a fresh move: histogram sign matches the move direction
    # (steady-state exponentials don't work here — a decaying series has
    # MACD rising toward zero, which flips the histogram positive).
    up = pd.Series([10.0] * 40 + [10.0 + 0.3 * i for i in range(1, 13)])
    _, _, h_up = I.macd(up)
    assert float(h_up.iloc[-1]) > 0
    dn = pd.Series([10.0] * 40 + [10.0 - 0.3 * i for i in range(1, 13)])
    _, _, h_dn = I.macd(dn)
    assert float(h_dn.iloc[-1]) < 0
    macd_line, signal_line, hist = I.macd(up)
    assert ((macd_line - signal_line - hist).abs() < 1e-12).all()


def test_momentum_screen_needs_enough_bars():
    ok, detail = I.momentum_screen_ok(pd.Series([10.0] * 10))
    assert ok is False and "insufficient bars" in str(detail.get("note"))


# ── indicators: TTM squeeze on/off ─────────────────────────────────────────
def test_squeeze_on_when_bb_inside_kc():
    df = _squeeze_regime()
    sq = I.ttm_squeeze_on(df["high"], df["low"], df["close"])
    assert bool(sq.iloc[-1]) is True


def test_squeeze_off_when_bb_outside_kc():
    df = _trending_regime()
    sq = I.ttm_squeeze_on(df["high"], df["low"], df["close"])
    assert bool(sq.iloc[-1]) is False


# ── indicators: upper-KC breakout alert ────────────────────────────────────
def _breakout_bars() -> pd.DataFrame:
    df = _squeeze_regime(n=60)
    bar = pd.DataFrame({"open": [10.0], "high": [12.1], "low": [9.9],
                        "close": [12.0], "volume": [90_000.0]})
    return pd.concat([df, bar], ignore_index=True)


def test_alert_fires_on_squeeze_plus_breakout_plus_rvol():
    st = I.alert_state(_breakout_bars(), rvol_value=5.0)
    assert st["squeeze_prev"] is True
    assert st["breakout"] is True
    assert st["rvol_ok"] is True
    assert st["alert"] is True
    assert st["close"] == 12.0 and st["kc_upper"] is not None


def test_alert_blocked_without_rvol():
    st = I.alert_state(_breakout_bars(), rvol_value=3.0)  # < RVOL_MIN 4.0
    assert st["rvol_ok"] is False and st["alert"] is False


def test_alert_blocked_without_squeeze():
    df = _trending_regime(n=60)
    bar = pd.DataFrame({"open": [df['close'].iloc[-1]], "high": [40.0],
                        "low": [27.0], "close": [39.0], "volume": [90_000.0]})
    st = I.alert_state(pd.concat([df, bar], ignore_index=True), rvol_value=5.0)
    assert st["squeeze_prev"] is False and st["alert"] is False


def test_alert_blocked_without_breakout():
    st = I.alert_state(_squeeze_regime(n=61), rvol_value=5.0)  # never breaks out
    assert st["breakout"] is False and st["alert"] is False


def test_alert_safe_on_bad_input():
    assert I.alert_state(None, rvol_value=5.0)["alert"] is False
    assert I.alert_state(_squeeze_regime(n=5), rvol_value=5.0)["alert"] is False


# ── indicators: RVOL ───────────────────────────────────────────────────────
def test_rvol_math():
    assert I.rvol(2_500_000, 500_000) == 5.0
    assert I.rvol(100_000, None) is None
    assert I.rvol(100_000, 0) is None
    assert I.rvol(None, 500_000) == 0.0


def test_bars_to_df_fmp_shape():
    bars = [{"t": 1, "o": 1.0, "h": 1.2, "l": 0.9, "c": 1.1, "v": 100},
            {"t": 2, "o": 1.1, "h": 1.3, "l": 1.0, "c": 1.2, "v": 200}]
    df = I.bars_to_df(bars)
    assert list(df["close"]) == [1.1, 1.2] and list(df["volume"]) == [100.0, 200.0]
    assert I.bars_to_df([]) is None and I.bars_to_df(None) is None


# ── screen: base filter (each rule accepts AND rejects) ────────────────────
def _good_row(**over):
    row = {"symbol": "ABCD", "price": 5.0, "volume": 1_000_000,
           "marketCap": 50_000_000, "exchangeShortName": "NASDAQ",
           "isEtf": False, "isActivelyTrading": True}
    row.update(over)
    return row


def test_base_screen_accepts_good_row():
    ok, why = passes_base_screen(_good_row())
    assert ok is True, why


def test_base_screen_rejects_each_rule():
    assert passes_base_screen(_good_row(price=0.50))[0] is False        # < $1
    assert passes_base_screen(_good_row(price=20.0))[0] is False        # > $15
    assert passes_base_screen(_good_row(volume=400_000))[0] is False    # <= 500k
    assert passes_base_screen(_good_row(marketCap=20_000_000))[0] is False
    assert passes_base_screen(_good_row(exchangeShortName="OTC"))[0] is False
    assert passes_base_screen(_good_row(exchangeShortName="PNK"))[0] is False
    assert passes_base_screen(_good_row(isEtf=True))[0] is False
    assert passes_base_screen(_good_row(isActivelyTrading=False))[0] is False


def test_normalize_exchange():
    assert normalize_exchange({"exchangeShortName": "NASDAQ"}) == "NASDAQ"
    assert normalize_exchange({"exchange": "NasdaqGS"}) == "NASDAQ"
    assert normalize_exchange({"exchangeShortName": "NYSE"}) == "NYSE"
    assert normalize_exchange({"exchange": "New York Stock Exchange"}) == "NYSE"
    assert normalize_exchange({"exchangeShortName": "OTC"}) == ""


# ── screen: anti-junk (incl. mandated fail-open) ───────────────────────────
def _good_cand(**over):
    cand = {"float_shares": 5_000_000, "market_cap": 50_000_000,
            "exchange": "NASDAQ", "ownership_evidence": True,
            "has_options_chain": True, "shortable": None}
    cand.update(over)
    return cand


def test_anti_junk_accepts_good_candidate():
    ok, reasons = passes_anti_junk(_good_cand())
    assert ok is True and any("float" in r for r in reasons)


def test_anti_junk_float_rule():
    assert passes_anti_junk(_good_cand(float_shares=25_000_000))[0] is False
    assert passes_anti_junk(_good_cand(float_shares=None))[0] is False  # hard screen
    assert passes_anti_junk(_good_cand(float_shares=19_999_999))[0] is True


def test_anti_junk_mcap_and_exchange_rules():
    assert passes_anti_junk(_good_cand(market_cap=10_000_000))[0] is False
    assert passes_anti_junk(_good_cand(market_cap=None))[0] is False
    assert passes_anti_junk(_good_cand(exchange=""))[0] is False
    assert passes_anti_junk(_good_cand(exchange="NYSE"))[0] is True


def test_anti_junk_ownership_fails_open():
    ok, reasons = passes_anti_junk(_good_cand(ownership_evidence=None))
    assert ok is True                       # MUST fail open (owner instruction)
    assert any("fail-open" in r for r in reasons)
    ok2, reasons2 = passes_anti_junk(_good_cand(ownership_evidence=True))
    assert ok2 is True and any("proxy" in r for r in reasons2)


def test_anti_junk_shortable_or_options():
    # verified no on BOTH legs -> reject
    assert passes_anti_junk(_good_cand(has_options_chain=False, shortable=False))[0] is False
    # unverifiable -> fail open with note
    ok, reasons = passes_anti_junk(_good_cand(has_options_chain=None, shortable=None))
    assert ok is True and any("fail-open" in r for r in reasons)
    assert passes_anti_junk(_good_cand(has_options_chain=False, shortable=True))[0] is True


# ── OI surge math ──────────────────────────────────────────────────────────
def test_oi_surge_math():
    pct = compute_oi_surge({"a": 100, "b": 100}, {"a": 200, "b": 110})
    assert pct is not None and abs(pct - 55.0) < 1e-9
    assert is_oi_surge(pct) is True
    assert is_oi_surge(compute_oi_surge({"a": 100}, {"a": 140})) is False  # 40% < 50%
    assert compute_oi_surge(None, {"a": 1}) is None      # no baseline -> no surge call
    assert compute_oi_surge({}, {"a": 1}) is None
    assert compute_oi_surge({"a": 0}, {"a": 50}) is None  # zero baseline
    assert is_oi_surge(None) is False


# ── levels: entry/stop/target are always present (NOT NULL columns) ────────
def test_build_levels_kc_fallback():
    lv = build_levels({"ticker": "ABCD", "price": 10.0, "bars": None},
                      {"close": 12.0, "kc_lower": 11.0, "bar_low": 11.5})
    assert lv["stop"] == 11.0 and lv["entry"] == 12.0
    assert abs(lv["target"] - (12.0 + C.TARGET_RR * 1.0)) < 1e-9
    assert lv["stop"] < lv["entry"] < lv["target"]


def test_build_levels_last_resort_never_null():
    lv = build_levels({"ticker": "ABCD", "price": 10.0, "bars": None},
                      {"close": 12.0, "kc_lower": None, "bar_low": None})
    assert all(lv[k] is not None and lv[k] > 0 for k in ("entry", "stop", "target"))
    assert lv["stop"] < lv["entry"] < lv["target"]
    # stop candidates ABOVE entry must be ignored, not used
    lv2 = build_levels({"ticker": "ABCD", "price": 10.0, "bars": None},
                       {"close": 12.0, "kc_lower": 13.0, "bar_low": 12.5})
    assert lv2["stop"] < lv2["entry"] < lv2["target"]


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


def test_safety_import_graph_clean_subprocess():
    """Import every saro12 module in a CLEAN interpreter and assert none of
    the email/pick/broker-order modules got imported. Subprocess so pytest
    conftest imports can't mask a violation."""
    code = (
        "import sys\n"
        "import app.engines.scanner.saro12.config\n"
        "import app.engines.scanner.saro12.indicators\n"
        "import app.engines.scanner.saro12.screen\n"
        "import app.engines.scanner.saro12.oi_snapshot\n"
        "import app.engines.scanner.saro12.shadow\n"
        f"bad = [m for m in {_FORBIDDEN_MODULES!r} if m in sys.modules]\n"
        "assert not bad, f'forbidden modules imported: {bad}'\n"
        "print('IMPORT-GRAPH-CLEAN')\n"
    )
    res = subprocess.run([sys.executable, "-c", code], cwd=_BACKEND,
                         capture_output=True, text=True, timeout=120)
    assert res.returncode == 0, f"stdout={res.stdout}\nstderr={res.stderr}"
    assert "IMPORT-GRAPH-CLEAN" in res.stdout


def test_safety_no_forbidden_tokens_in_source():
    """No saro12 source file may IMPORT an email/pick module or CALL an
    email/pick/order function (docstring mentions are fine — this scans
    import statements and call sites)."""
    pkg = os.path.join(_BACKEND, "app", "engines", "scanner", "saro12")
    for fn in sorted(os.listdir(pkg)):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(pkg, fn), encoding="utf-8").read()
        m = _FORBIDDEN_IMPORT_RE.search(src)
        assert m is None, f"{fn}: forbidden import: {m.group(0)!r}"
        m = _FORBIDDEN_CALL_RE.search(src)
        assert m is None, f"{fn}: forbidden call site: {m.group(0)!r}"


def test_safety_rows_are_unmistakably_shadow():
    """The INSERT in shadow.py must hard-code shadow=true, watch_only, the
    saro12 source tag and the saro12: strategy prefix."""
    src = open(os.path.join(_BACKEND, "app", "engines", "scanner", "saro12",
                            "shadow.py"), encoding="utf-8").read()
    assert re.search(r"INSERT INTO email_signals_history", src)
    assert re.search(r"VALUES\s*\(NOW\(\),[^;]*true", src), "shadow=true not hard-coded"
    assert "'watch_only'" in src
    assert C.SOURCE_TAG == "saro12_shadow"
    assert C.MATCHED_STRATEGY.startswith(C.MATCHED_STRATEGY_PREFIX)
    # exactly one INSERT statement in the whole package
    pkg = os.path.join(_BACKEND, "app", "engines", "scanner", "saro12")
    inserts = sum(open(os.path.join(pkg, f), encoding="utf-8").read()
                  .count("INSERT INTO") for f in os.listdir(pkg) if f.endswith(".py"))
    assert inserts == 1


def test_spawn_check_gates_weekend_and_window():
    """maybe_spawn_saro12_shadow must self-gate (the fast loop runs 24/7)."""
    from datetime import datetime
    from app.engines.scanner.saro12.shadow import maybe_spawn_saro12_shadow
    sat = datetime(2026, 8, 8, 10, 0, 0)     # Saturday
    assert maybe_spawn_saro12_shadow(_now_et_fn=lambda: sat) is False
    pre = datetime(2026, 8, 6, 9, 0, 0)      # weekday, before 09:33
    assert maybe_spawn_saro12_shadow(_now_et_fn=lambda: pre) is False
    lunch = datetime(2026, 8, 6, 12, 0, 0)   # 11:00-18:30 dead zone
    assert maybe_spawn_saro12_shadow(_now_et_fn=lambda: lunch) is False
    os.environ[C.ENV_GATE] = "0"             # env gate blocks even in-window
    try:
        inwin = datetime(2026, 8, 6, 9, 40, 0)
        assert maybe_spawn_saro12_shadow(_now_et_fn=lambda: inwin) is False
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
