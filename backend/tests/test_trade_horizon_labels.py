"""TRADE-HORIZON-V1 tests — Day/Swing label on every entry-email surface.

Covers:
  1. get_trade_horizon hard rules (stock_pick always day; only explicit
     'swing' swings; NULL/garbage -> day).
  2. send_trade_receipt_email: pill + action line + subject suffix for day
     and swing; the `reason` clobber regression (firewall session label used
     to overwrite the trade rationale in the "Why:" rows).
  3. send_signal_email (futures): pill + action line + subject suffix.
  4. emit_theta_pick (stock pick): hard-coded DAY pill + 3:55 PM ET action
     line; WATCH-ONLY picks get NO pill/action line and NO subject suffix.
  5. Killswitch whitelist: every suffixed subject still passes; the suffix
     alone can never whitelist a random subject.
  6. fetch_strategy_horizon fail-safe: DB unreachable -> 'day'.

Run standalone (throwaway container, EMAIL_KILL_SWITCH=1, no creds):
    python tests/test_trade_horizon_labels.py

EMAIL SAFETY: no test sends a real email — every sender (_send /
_send_tracked) is monkeypatched to capture, and the container has no
RESEND_API_KEY. Asserts run on returned/rendered HTML only, never delivery.
"""
import asyncio
import os
import sys
import types

os.environ.setdefault("EMAIL_KILL_SWITCH", "1")
os.environ.setdefault("RESEND_API_KEY", "")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PASS: list = []
_FAIL: list = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        _PASS.append(name)
        print(f"PASS  {name}")
    else:
        _FAIL.append(name)
        print(f"FAIL  {name}  {detail}")


DAY_SUFFIX = " · Day Trade"
SWING_SUFFIX = " · Swing Trade"
DAY_MARKERS = ("DAY TRADE", "3:55 PM ET", "flat before the close")
# Futures/receipt-on-futures day surface: honest copy — no auto-close claim.
DAY_MARKERS_FUT = ("DAY TRADE", "NOT auto-close futures", "flat within your session")
SWING_MARKERS = ("SWING TRADE", "holding overnight", "weekends")


# ─── 1. get_trade_horizon hard rules ─────────────────────────────────────────
def test_horizon_resolution():
    from app.services.trade_horizon import get_trade_horizon as gth

    check("resolve: None -> day", gth(None) == "day")
    check("resolve: 'day' -> day", gth("day") == "day")
    check("resolve: 'swing' -> swing", gth("swing") == "swing")
    check("resolve: ' SWING ' normalized -> swing", gth(" SWING ") == "swing")
    check("resolve: garbage -> day", gth("overnight") == "day")
    check("resolve: row.trade_horizon='swing' -> swing",
          gth(types.SimpleNamespace(trade_horizon="swing")) == "swing")
    check("resolve: row.trade_horizon=None -> day",
          gth(types.SimpleNamespace(trade_horizon=None)) == "day")
    check("resolve: row without attr -> day",
          gth(types.SimpleNamespace()) == "day")
    check("resolve: stock_pick source ALWAYS day (even swing row)",
          gth(types.SimpleNamespace(trade_horizon="swing"), source="stock_pick") == "day")
    check("resolve: stock_pick source ALWAYS day (even 'swing' str)",
          gth("swing", source="stock_pick") == "day")


# ─── fragments ───────────────────────────────────────────────────────────────
def test_fragments():
    from app.services.trade_horizon import (
        horizon_block_html, horizon_action_line, horizon_subject_suffix,
    )
    day_html = horizon_block_html("day")
    swing_html = horizon_block_html("swing")
    check("fragment: day pill html has all day markers",
          all(m in day_html for m in DAY_MARKERS), day_html[:120])
    check("fragment: day copy says 15:55 auto-close (never 4:30)",
          "15:55" in day_html and "4:30" not in day_html)
    check("fragment: swing pill html has all swing markers",
          all(m in swing_html for m in SWING_MARKERS), swing_html[:120])
    check("fragment: day action line imperative",
          "be flat before the close" in horizon_action_line("day"))
    check("fragment: swing action line says bot manages exit",
          "The bot manages the exit" in horizon_action_line("swing"))
    check("fragment: subject suffixes",
          horizon_subject_suffix("day") == DAY_SUFFIX
          and horizon_subject_suffix("swing") == SWING_SUFFIX
          and horizon_subject_suffix("junk") == DAY_SUFFIX)


# ─── 5. killswitch whitelist safety ──────────────────────────────────────────
def test_killswitch_subjects():
    from app.services import email as email_mod
    allows = email_mod._killswitch_allows

    subjects_must_pass = [
        "\U0001F3AF Saro — Today's Pick: TSLA +8% target" + DAY_SUFFIX,
        "\U0001F3AF Saro: \U0001F440 WATCH ONLY — TSLA (no clean setup today)",
        "\U0001F3AF Saro (Futures): LONG NQ @ 23000.00 · ICT Silver Bullet" + SWING_SUFFIX,
        "\U0001F525 Saro Signal · LONG NQ @ 23000.00 (+0.4% target)" + DAY_SUFFIX,
        "Daily summary - Thu, Jun 4, 2026 - -660.00 P&L",  # transactional regression
    ]
    for s in subjects_must_pass:
        check(f"killswitch passes: {s[:52]!r}", allows(s))
    check("killswitch: suffix alone must NOT whitelist a random subject",
          not allows("position log" + DAY_SUFFIX) and not allows("position log" + SWING_SUFFIX))

    # Real-path drop check (no network: killswitch rejects before any send).
    res = email_mod._send_tracked("nobody@example.com", "random test subject", "<p></p>")
    check("killswitch real-path: non-whitelist subject dropped",
          res.get("sent") is False and res.get("provider_status") == "killswitch_dropped",
          str(res))


# ─── 2. send_trade_receipt_email ─────────────────────────────────────────────
def test_receipt_email():
    from app.services import email as email_mod

    captured = {}

    def _fake_send(to, subject, html):
        captured["to"], captured["subject"], captured["html"] = to, subject, html
        return True

    orig_send, orig_fw = email_mod._send, email_mod._fw_check
    email_mod._send = _fake_send
    email_mod._fw_check = lambda to: (True, "NY_AM")
    try:
        reason_text = "VWAP reclaim + liquidity sweep of Asia low"
        ok = email_mod.send_trade_receipt_email(
            to="u@example.com", username="u", ticker="NQ", direction="long",
            entry=23000.0, stop=22950.0, target=23100.0, contracts=2,
            reason=reason_text, strategy_name="ICT Silver Bullet", mode="paper",
        )
        check("receipt(day): returns True", ok is True)
        check("receipt(day): subject keeps whitelisted prefix + Day suffix",
              "Saro Signal" in captured["subject"] and captured["subject"].endswith(DAY_SUFFIX),
              captured.get("subject", ""))
        check("receipt(day): killswitch still passes suffixed subject",
              email_mod._killswitch_allows(captured["subject"]))
        check("receipt(day): pill + action line rendered (futures surface — honest copy)",
              all(m in captured["html"] for m in DAY_MARKERS_FUT))
        check("receipt(day): 'Why' rows show the trade rationale (clobber fix)",
              reason_text in captured["html"] and "NY_AM" not in captured["html"])

        captured.clear()
        ok = email_mod.send_trade_receipt_email(
            to="u@example.com", username="u", ticker="NQ", direction="short",
            entry=23000.0, stop=23050.0, target=22900.0, contracts=1,
            reason="weekly FVG rebalance", strategy_name="HTF Swing",
            mode="paper", trade_horizon="swing",
        )
        check("receipt(swing): returns True", ok is True)
        check("receipt(swing): subject Swing suffix",
              captured["subject"].endswith(SWING_SUFFIX), captured.get("subject", ""))
        check("receipt(swing): pill + action line rendered",
              all(m in captured["html"] for m in SWING_MARKERS))
        check("receipt(swing): no day action line",
              "flat before the close" not in captured["html"])

        # Firewall drop still short-circuits before building/sending anything.
        captured.clear()
        email_mod._fw_check = lambda to: (False, "DEAD_ZONE")
        ok = email_mod.send_trade_receipt_email(
            to="u@example.com", username="u", ticker="ES", direction="long",
            entry=5200.0, stop=5190.0, target=5230.0, contracts=1,
            reason="x", strategy_name="s",
        )
        check("receipt: firewall drop returns False, nothing sent",
              ok is False and "subject" not in captured)
    finally:
        email_mod._send, email_mod._fw_check = orig_send, orig_fw


# ─── 3. send_signal_email (futures watcher surface) ──────────────────────────
def test_futures_signal_email():
    import pandas as pd
    from app.api.routes import account_signals as acct_mod
    from app.services.email import _killswitch_allows
    import app.services.trade_chart as chart_mod

    captured = {}

    def _fake_tracked(to, subject, html, signal_id=None, inline_png=None, inline_cid="tradechart"):
        captured["to"], captured["subject"], captured["html"] = to, subject, html
        return {"sent": True, "provider_message_id": "stub-id",
                "provider_status": "sent", "error": None, "latency_ms": 1}

    _one_row = pd.DataFrame([{
        "timestamp": pd.Timestamp.now(tz="UTC"), "open": 1.0, "high": 1.0,
        "low": 1.0, "close": 1.0, "volume": 1,
    }])

    orig_tracked = acct_mod._send_tracked
    orig_window = acct_mod._candle_cache_window_df
    orig_chart = chart_mod.generate_trade_chart
    acct_mod._send_tracked = _fake_tracked
    acct_mod._candle_cache_window_df = lambda instrument, start, end: _one_row
    chart_mod.generate_trade_chart = lambda **kw: None  # no matplotlib in tests
    try:
        common = dict(
            to="u@example.com", username="u", account_label="Apex 50k",
            strategy_name="ICT Silver Bullet", instrument="NQ", direction="long",
            entry=23000.0, stop=22950.0, target=23100.0, bias="bullish",
            fired_at="Fri, Aug 8 9:45 AM ET",
            stop_reason="swept Asia low", target_reason="HTF FVG",
        )
        res = acct_mod.send_signal_email(signal_id="th-test-1", **common)
        check("futures(day-default): send path completed", bool(res.get("sent")), str(res)[:160])
        check("futures(day-default): subject keeps prefix + Day suffix",
              captured["subject"].startswith("\U0001F3AF Saro (Futures):")
              and captured["subject"].endswith(DAY_SUFFIX),
              captured.get("subject", ""))
        check("futures(day-default): pill + action line rendered (honest copy)",
              all(m in captured["html"] for m in DAY_MARKERS_FUT))

        captured.clear()
        res = acct_mod.send_signal_email(signal_id="th-test-2", trade_horizon="swing", **common)
        check("futures(swing): subject Swing suffix",
              captured["subject"].endswith(SWING_SUFFIX), captured.get("subject", ""))
        check("futures(swing): pill + action line rendered",
              all(m in captured["html"] for m in SWING_MARKERS))
        check("futures(swing): killswitch still passes suffixed subject",
              _killswitch_allows(captured["subject"]))

        captured.clear()
        res = acct_mod.send_signal_email(signal_id="th-test-3", trade_horizon="garbage", **common)
        check("futures(garbage horizon): safe default day",
              captured["subject"].endswith(DAY_SUFFIX)
              and all(m in captured["html"] for m in DAY_MARKERS_FUT))
    finally:
        acct_mod._send_tracked = orig_tracked
        acct_mod._candle_cache_window_df = orig_window
        chart_mod.generate_trade_chart = orig_chart


# ─── 4. emit_theta_pick (stock pick surface) ─────────────────────────────────
def _pick(watch: bool) -> dict:
    return {
        "ticker": "TSLA", "price": 250.0, "entry": 251.0, "stop": 245.0,
        "target": 270.0, "gap_pct": 4.2, "rel_vol": 3.1, "today_vol": 1_200_000,
        "score": 9.1, "catalyst_reason": "earnings beat",
        "projected_move_pct": 7.5, "alternatives": [],
        "quality_reasons": ["above VWAP"], "watch_only": watch,
    }


def test_stock_pick_email():
    from app.services import email as email_mod
    from app.engines.options import theta_scanner as ts_mod
    from app.engines.options import premarket_scheduler as pm_mod
    import app.services.trade_chart as chart_mod
    import app.engines.level_reasons as lr_mod

    captured = {}

    def _fake_tracked(to, subject, html, signal_id=None, inline_png=None, inline_cid="tradechart"):
        captured["to"], captured["subject"], captured["html"] = to, subject, html
        return {"sent": True, "provider_message_id": "stub-id",
                "provider_status": "sent", "error": None, "latency_ms": 1}

    async def _no_bars(*a, **kw):
        return []

    async def _no_broker(*a, **kw):
        return (None, "paper")

    orig = (email_mod._send_tracked, pm_mod._polygon_1min_bars,
            pm_mod._resolve_user_broker, chart_mod.generate_trade_chart,
            lr_mod.infer_stop_target_reasons)
    email_mod._send_tracked = _fake_tracked
    pm_mod._polygon_1min_bars = _no_bars
    pm_mod._resolve_user_broker = _no_broker
    chart_mod.generate_trade_chart = lambda **kw: None
    lr_mod.infer_stop_target_reasons = lambda **kw: {}
    user = types.SimpleNamespace(
        email="ryan@example.com", username="ryan",
        id="00000000-0000-0000-0000-000000000001",
    )
    try:
        ok = asyncio.run(ts_mod.emit_theta_pick(None, user, _pick(watch=False)))
        check("stock pick: emit returned sent=True", ok is True, str(ok))
        check("stock pick: subject keeps prefix + HARD-CODED Day suffix",
              captured["subject"].startswith("\U0001F3AF Saro — Today's Pick: TSLA")
              and captured["subject"].endswith(DAY_SUFFIX),
              captured.get("subject", ""))
        check("stock pick: killswitch still passes suffixed subject",
              email_mod._killswitch_allows(captured["subject"]))
        check("stock pick: DAY pill + 3:55 PM ET action line rendered",
              all(m in captured["html"] for m in DAY_MARKERS))
        check("stock pick: never a swing label",
              "SWING TRADE" not in captured["html"]
              and "Swing Trade" not in captured["subject"])

        captured.clear()
        asyncio.run(ts_mod.emit_theta_pick(None, user, _pick(watch=True)))
        check("watch-only: subject unchanged (no horizon suffix)",
              captured["subject"].startswith("\U0001F3AF Saro:")
              and "Day Trade" not in captured["subject"],
              captured.get("subject", ""))
        check("watch-only: NO trade-type pill / action line (not a trade)",
              "DAY TRADE" not in captured["html"]
              and "flat before the close" not in captured["html"])
        check("watch-only: WATCH ONLY banner still present",
              "WATCH ONLY" in captured["html"])
    finally:
        (email_mod._send_tracked, pm_mod._polygon_1min_bars,
         pm_mod._resolve_user_broker, chart_mod.generate_trade_chart,
         lr_mod.infer_stop_target_reasons) = orig


# ─── 6. fetch_strategy_horizon fail-safe ─────────────────────────────────────
def test_fetch_horizon_failsafe():
    from app.services.trade_horizon import fetch_strategy_horizon

    check("fetch: no strategy_id -> day",
          asyncio.run(fetch_strategy_horizon(None)) == "day")
    # DB is unreachable in the test container: lookup must fall back to 'day'
    # (and must do so quickly — internal 3s cap).
    got = asyncio.run(fetch_strategy_horizon("00000000-0000-0000-0000-000000000000"))
    check("fetch: DB unreachable -> day (fail-safe)", got == "day", got)


if __name__ == "__main__":
    test_horizon_resolution()
    test_fragments()
    test_killswitch_subjects()
    test_receipt_email()
    test_futures_signal_email()
    test_stock_pick_email()
    test_fetch_horizon_failsafe()
    print(f"\n{len(_PASS)} passed, {len(_FAIL)} failed")
    if _FAIL:
        print("FAILED:", *_FAIL, sep="\n  - ")
        sys.exit(1)
    print("ALL TESTS PASSED")
