"""Saro SUNDAY WATCHLIST — top-5 coiled names for the coming week (WATCH ONLY).

Ryan's ask (2026-08-18): a Sunday-evening list of names "about to pop" — the
STT-Oracle / Tim-Bohen-Sunday-list shape: anticipation, not reaction. His own
shadow-template tracker made the case since 6/24: all 12 reactive momentum
templates are negative (14-26% WR). This cohort bets on ANTICIPATION — find
the coil on Friday's completed daily bars and watch it through the week.

KEY DESIGN DIFFERENCE vs the saro13 daily shadow: the watchlist RANKS instead
of hard-gating. saro13's run_screen only passes names that clear EVERY S1/S2
gate; a watchlist can be softer. Here every anti-junk-passing name with daily
history gets a weighted COMPRESSION SCORE (0-100, see WEIGHTS):

    bb_width         BB-width tightness — strict percentile rank of the
                     current width inside its trailing 60-session range,
                     inverted (tightest coil -> 1.0)
    atr_streak       ATR(14) declining-streak length, capped at
                     ATR_STREAK_CAP sessions (streak/cap)
    rsi_neutral      RSI(14) closeness to 50 (1.0 at 50, 0.0 at <=25 / >=75)
    vol_contraction  volume-contraction depth vs SMA20 (ratio >=1.0 -> 0.0,
                     <=0.5 -> 1.0)
    float_tight      float-tightness PROXY: shares outstanding approximated
                     as marketCap / Friday close, log-scaled (<=50M -> 1.0,
                     >=5B -> 0.0). The FMP screener row carries no float
                     field and a per-ticker float endpoint would blow the
                     30-call budget — this proxy is honest and free.

The top TOP_N names by score (M&A HARD exclude still applies, fail-closed on
a confirmed hit — saro13.screen.ma_gate reused verbatim) go out in ONE
Sunday-evening email, each labeled with WHICH strict coil criteria it meets,
and become shadow rows the existing outcome resolver scores against next
week's real candles. That is how the "~20% pop" claim gets verified against
reality instead of vibes.

WEEKEND DATA: a Sunday run needs NO intraday data — Friday EOD daily bars are
complete, and indicators.daily_rows_to_df(today_et=<Sunday>) keeps every
Friday bar. FRIDAY-CACHE HONESTY: saro13's redis caches are keyed per-ET-day,
and reusing Friday's keys here would be WRONG anyway — Friday's cached
screener/EOD payloads were fetched 09:33-11:00 ET Friday, so Friday's own bar
inside them is a LIVE PARTIAL, not the completed close this list anchors on
(entry = Friday close). The Sunday run therefore pulls fresh under its own
Sunday keys with saro13's exact budget discipline: 1 screener + <=30 EOD
(the shared budget dict / DAILY_HISTORY_CAP) + 1 M&A list + <=10 news checks
= <=42 calls worst case, and a same-evening rerun after a restart costs ~0
via the Sunday-keyed caches.

DELIVERY WINDOW: Sundays 18:00:00-19:30:00 ET (weekday()==6 — the INVERSE of
the weekday cohorts' weekend skip), redis latch theta:sunwatch:{ET-date},
env gate SARO13_SUNWATCH_ENABLED (default on). The window end is pinned
STRICTLY before 20:00 ET: picked_at is stamped NOW() in UTC, and past 20:00
ET the UTC date rolls to tomorrow — rows would read future-dated from ET and
escape the ET-day dedup (the 2026-08-17 anomaly mechanism).

WATCH ONLY — ABSOLUTE:
  * The email is a WATCHLIST (ideas to watch), NOT trade signals: no
    entry/stop/target framed as orders, no Confirm/Skip links, explicit
    not-a-trade-signal language (NOT_A_SIGNAL_LINE).
  * NEVER queues paper/live entries; never imports pick_router /
    emit_theta_pick / broker order paths (tests/test_sunday_watchlist.py
    asserts the import graph AND scans call sites).
  * The ONE deliberate difference from the rest of the saro13 package: this
    module SENDS EMAIL — via app.services.email._send only (LAZY import at
    send time, so the module import graph stays email-free; the killswitch
    whitelist passes the subject because it contains "Saro"), to the
    SARO-ACTIVATED recipient set only (app.core.saro.SARO_RECIPIENT_WHERE —
    the same gate as the daily pick). Deliberately NOT
    send_consolidated_signals_email: that path runs the per-recipient
    session firewall (a Sunday 18:xx send could be dropped) and renders
    Confirm/Skip pending-trade links, both forbidden here.
  * Shadow rows: shadow=true, source='sunwatch',
    matched_strategy='saro13:sunday_watchlist', instrument_type='watch_only',
    entry=Friday close, stop/target derived by saro13's build_levels (so the
    resolver can score outcomes — levels are bookkeeping, not orders).
"""
from __future__ import annotations

import json
import html as _html
import math
import os
from datetime import datetime, timezone
from typing import Optional

from loguru import logger  # stdlib INFO invisible in prod
import pandas as pd

from app.engines.scanner.saro13 import config as C
from app.engines.scanner.saro13 import indicators as I
# build_levels: same entry/stop/target derivation as the daily cohort.
# _ensure_saro13_columns: prod email_signals_history is STILL missing
# source/detection_ts (verified 2026-08-18) — ensure before EVERY insert.
# _spawn_retained: the GC-proof task-retention pattern (2026-08-07 incident —
# a naked asyncio.create_task is a blocker); this module never calls
# asyncio.create_task directly.
from app.engines.scanner.saro13.shadow import (_ensure_saro13_columns,
                                               _spawn_retained, build_levels)
from app.engines.scanner.saro13.screen import (_bulk_screener,
                                               _fetch_daily_history,
                                               _news_headlines, ma_gate,
                                               passes_base_screen)

# ── identity / scheduling (local on purpose — nothing else shares these) ───
ENV_GATE = "SARO13_SUNWATCH_ENABLED"        # default "1"
LATCH_FMT = "theta:sunwatch:{date}"         # SETNX latch, TTL C.LATCH_TTL_S
SPAWN_START_S = 18 * 3600                   # 18:00:00 ET Sunday
SPAWN_END_S = 19 * 3600 + 30 * 60           # 19:30:00 ET — MUST stay < 20:00
                                            # ET (UTC-dateline dedup pin)
TOP_N = 5
MATCHED_STRATEGY = "saro13:sunday_watchlist"   # keeps the saro13: readout prefix
SOURCE_TAG = "sunwatch"
SUBJECT_FMT = "\U0001f52e Saro Weekly Watch — {n} coiled name{plural} for the week"
NOT_A_SIGNAL_LINE = ("WATCHLIST — NOT TRADE SIGNALS. Nothing here is an "
                     "entry, an order, or advice; no trades are queued.")

# ── compression-score weighting (sums to 1.0 — pinned in tests) ────────────
WEIGHTS = {
    "bb_width": 0.30,
    "atr_streak": 0.20,
    "rsi_neutral": 0.15,
    "vol_contraction": 0.20,
    "float_tight": 0.15,
}
ATR_STREAK_CAP = 10                 # sessions of decline for a full 1.0
FLOAT_SHARES_TIGHT = 5e7            # <= 50M shares (proxy) -> 1.0
FLOAT_SHARES_WIDE = 5e9             # >= 5B shares (proxy) -> 0.0


# ── scoring components (pure, deterministic — unit-tested on synthetics) ───
def bb_width_rank_score(closes: pd.Series) -> tuple[float, dict]:
    """1.0 = tightest coil. STRICT percentile rank (fraction of the trailing
    60 widths strictly below the current one — ties don't punish a flat-line
    coil), inverted. Insufficient history -> 0.0 with a note."""
    detail: dict = {"width_pct_rank": None, "bb_width": None}
    w = I.bb_width(closes).dropna()
    if len(w) < C.BBWIDTH_RANGE_SESSIONS:
        detail["note"] = (f"insufficient width history "
                          f"({len(w)} < {C.BBWIDTH_RANGE_SESSIONS})")
        return 0.0, detail
    window = w.iloc[-C.BBWIDTH_RANGE_SESSIONS:].to_numpy(dtype=float)
    cur = float(window[-1])
    rank = float((window < cur).mean()) * 100.0
    detail["width_pct_rank"] = round(rank, 2)
    detail["bb_width"] = round(cur, 6)
    return round(1.0 - rank / 100.0, 4), detail


def atr_streak_score(atr_series: pd.Series,
                     cap: int = ATR_STREAK_CAP) -> tuple[float, int]:
    """Length of the strictly-decreasing ATR suffix run (in VALUES, matching
    saro13's 'declining 5 consecutive sessions' = 5 values), capped at `cap`.
    Returns (score in [0,1], raw streak length)."""
    a = atr_series.dropna().to_numpy(dtype=float)
    if len(a) == 0:
        return 0.0, 0
    streak = 1
    for i in range(len(a) - 1, 0, -1):
        if a[i] < a[i - 1]:
            streak += 1
        else:
            break
    return round(min(streak, cap) / float(cap), 4), streak


def rsi_neutrality_score(rsi_val) -> float:
    """1.0 at RSI 50, linear to 0.0 at RSI <= 25 or >= 75. Unknown -> 0.0."""
    try:
        r = float(rsi_val)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, 1.0 - abs(r - 50.0) / 25.0), 4)


def vol_contraction_score(vol_ratio) -> float:
    """Depth of contraction: ratio = last volume / SMA20(volume).
    >= 1.0 (no contraction) -> 0.0; <= 0.5 (half average) -> 1.0."""
    try:
        vr = float(vol_ratio)
    except (TypeError, ValueError):
        return 0.0
    if vr <= 0:
        return 0.0
    return round(max(0.0, min(1.0, (1.0 - vr) / 0.5)), 4)


def float_tightness_score(mcap, price) -> tuple[float, Optional[float]]:
    """FLOAT PROXY (the screener row has no float field): shares outstanding
    ~= marketCap / price, log-scaled between FLOAT_SHARES_TIGHT (1.0) and
    FLOAT_SHARES_WIDE (0.0). Returns (score, shares_proxy)."""
    try:
        m, p = float(mcap or 0.0), float(price or 0.0)
    except (TypeError, ValueError):
        return 0.0, None
    if m <= 0 or p <= 0:
        return 0.0, None
    shares = m / p
    lo, hi = math.log10(FLOAT_SHARES_TIGHT), math.log10(FLOAT_SHARES_WIDE)
    s = (hi - math.log10(shares)) / (hi - lo)
    return round(max(0.0, min(1.0, s)), 4), shares


def compression_score(parts: dict) -> float:
    """Weighted sum of the [0,1] component scores -> 0-100."""
    total = 0.0
    for k, wgt in WEIGHTS.items():
        try:
            total += wgt * max(0.0, min(1.0, float(parts.get(k) or 0.0)))
        except (TypeError, ValueError):
            continue
    return round(total * 100.0, 2)


def score_candidate(df: pd.DataFrame, st: dict, mcap) -> dict:
    """All five components + the weighted score for one name. `st` is
    indicators.compression_state(df) — it computes every component input
    BEFORE its hard gates, which is exactly what lets this ranked pass score
    names the strict daily cohort would reject."""
    bb_s, bb_det = bb_width_rank_score(df["close"])
    atr_s, streak = atr_streak_score(I.atr(df["high"], df["low"], df["close"]))
    flt_s, shares = float_tightness_score(mcap, st.get("close"))
    parts = {"bb_width": bb_s, "atr_streak": atr_s,
             "rsi_neutral": rsi_neutrality_score(st.get("rsi")),
             "vol_contraction": vol_contraction_score(st.get("vol_ratio")),
             "float_tight": flt_s}
    return {"score": compression_score(parts), "parts": parts,
            "atr_streak_len": streak,
            "width_pct_rank": bb_det.get("width_pct_rank"),
            "shares_proxy": shares}


def criteria_hit(st: dict) -> list[str]:
    """Which of saro13's STRICT coil criteria this name actually meets —
    the honesty labels on a soft-ranked list."""
    hits: list[str] = []
    if st.get("bb_width_ok"):
        hits.append(f"BB squeeze (width in lowest {C.BBWIDTH_LOWEST_PCT:g}% "
                    f"of {C.BBWIDTH_RANGE_SESSIONS}d)")
    if st.get("atr_declining_ok"):
        hits.append(f"ATR down {C.ATR_DECLINE_SESSIONS}+ straight sessions")
    if st.get("rsi_ok"):
        hits.append(f"RSI neutral ({st.get('rsi')} in "
                    f"[{C.RSI_BAND_LO:g},{C.RSI_BAND_HI:g}])")
    if st.get("vol_ok"):
        hits.append(f"volume contraction (ratio {st.get('vol_ratio')} "
                    f"vs SMA{C.VOL_SMA_PERIOD})")
    lc = st.get("last_chg_pct")
    if lc is not None and lc <= C.DAILY_CHANGE_MAX_PCT:
        hits.append(f"not yet breaking (Fri {lc:+.2f}% <= "
                    f"+{C.DAILY_CHANGE_MAX_PCT:g}%)")
    return hits


def rank_watchlist(scored: list[dict],
                   top_n: Optional[int] = None) -> list[dict]:
    """Deterministic ranking: score desc, ticker asc tiebreak. Input order
    can never change the result (stability invariant pinned in tests)."""
    ranked = sorted(scored, key=lambda c: (-float(c.get("score") or 0.0),
                                           str(c.get("ticker") or "")))
    return ranked if top_n is None else ranked[:top_n]


# ── the ranked funnel (recomposed from saro13.screen pieces) ───────────────
async def build_watchlist(r, date_key: str, budget: dict) -> list[dict]:
    """screener -> anti-junk base screen -> dollar-volume presort ->
    budget-capped daily histories -> compression SCORE (no gate) -> rank ->
    M&A HARD exclude top-down -> top TOP_N. Never raises; failures shrink
    the list. run_screen itself is unusable here — it hard-gates on
    st['compressed'] + live daily change."""
    try:
        rows = await _bulk_screener(r, date_key)
        base = []
        for row in rows:
            sym = str(row.get("symbol") or "").strip().upper()
            if not sym or not sym.isalnum():
                continue
            ok, _why = passes_base_screen(row)
            if ok:
                base.append(row)
        logger.info(f"[sunwatch] base screen: {len(base)}/{len(rows)} rows pass")
        # same deterministic presort as the daily cohort: dollar volume desc
        # (most liquid names first — the 30-pull cap decides who gets scored)
        base.sort(key=lambda x: float(x.get("price") or 0) * float(x.get("volume") or 0),
                  reverse=True)

        scored: list[dict] = []
        for row in base:
            if budget.get("cap_hit"):
                break
            sym = str(row["symbol"]).upper()
            hist = await _fetch_daily_history(r, date_key, sym, budget)
            if budget.get("cap_hit"):
                logger.info(f"[sunwatch] daily-history cap "
                            f"({C.DAILY_HISTORY_CAP}) reached — scoring stops")
                break
            if not hist:
                continue
            df = I.daily_rows_to_df(hist, today_et=date_key)
            st = I.compression_state(df)
            if df is None or st.get("close") is None:
                logger.debug(f"[sunwatch] {sym}: unusable history — skip")
                continue
            cand = {"ticker": sym,
                    "close": st["close"], "prev_close": st["prev_close"],
                    "last_chg_pct": st["last_chg_pct"],
                    "upper_bb": st["upper_bb"], "lower_bb": st["lower_bb"],
                    "swing_low": st["swing_low"],
                    "compression_range": st["compression_range"],
                    "rsi": st["rsi"], "atr": st["atr"],
                    "vol_ratio": st["vol_ratio"],
                    "friday_vol": st["vol"],
                    "criteria": criteria_hit(st),
                    "fully_coiled": bool(st["compressed"])}
            cand.update(score_candidate(df, st, row.get("marketCap")))
            scored.append(cand)
        logger.info(f"[sunwatch] scored {len(scored)} names "
                    f"({budget.get('eod_pulls', 0)} fresh EOD pulls)")

        # M&A hard exclude (fail-CLOSED on a confirmed hit) applied top-down
        # so news checks are only spent on names that could actually make it
        final: list[dict] = []
        for cand in rank_watchlist(scored):
            if len(final) >= TOP_N:
                break
            excluded, ma_note = await ma_gate(r, date_key, cand["ticker"], budget)
            if excluded:
                logger.info(f"[sunwatch] {cand['ticker']} M&A exclude: {ma_note}")
                continue
            cand["ma_note"] = ma_note
            try:  # catalyst note, free: ma_gate just fetched+cached headlines
                arts = await _news_headlines(r, date_key, cand["ticker"], budget)
                if arts:
                    title = str((arts[0] or {}).get("title") or "").strip()
                    if title:
                        cand["catalyst"] = title
            except Exception:
                pass
            cand["rank"] = len(final) + 1
            final.append(cand)
        logger.info(f"[sunwatch] final watchlist: "
                    f"{[c['ticker'] for c in final]} "
                    f"(scores {[c['score'] for c in final]})")
        return final
    except Exception as e:
        logger.error(f"[sunwatch] build_watchlist failed: {e}")
        return []


# ── persistence (resolver-scored verification rows) ────────────────────────
async def persist_watch_row(db, cand: dict, detection_ts: datetime) -> bool:
    """INSERT one Sunday-watch shadow row (saro13 column shape — the existing
    outcome resolver scores it against next week's daily candles with zero
    changes). Dedup: one (ticker, saro13:sunday_watchlist, ET-day) row.
    NOTE: sunwatch rows expire on CALENDAR days from Sunday, so they get ~4
    trading days of runway vs the weekday cohorts' ~5 — flag in readouts."""
    from sqlalchemy import text as _t
    tk = cand["ticker"]
    dup = (await db.execute(_t(
        "SELECT 1 FROM email_signals_history WHERE ticker=:tk AND matched_strategy=:ms "
        "AND picked_at::date = (NOW() AT TIME ZONE 'America/New_York')::date LIMIT 1"
    ), {"tk": tk, "ms": MATCHED_STRATEGY})).first()
    if dup:
        logger.info(f"[sunwatch] {tk} already recorded today — skip")
        return False
    lv = build_levels(cand)  # entry = Friday close; saro13 stop/target derivation
    quality = list(cand.get("criteria") or [])
    quality.append(f"compression score {cand.get('score')}/100 — parts "
                   f"{json.dumps(cand.get('parts') or {})} x weights "
                   f"{json.dumps(WEIGHTS)}")
    if cand.get("catalyst"):
        quality.append(f"catalyst note: {str(cand['catalyst'])[:160]}")
    why = [(f"Saro Sunday watchlist #{cand.get('rank')}: coiled name to WATCH "
            f"next week — compression score {cand.get('score')}/100 (width "
            f"rank p{cand.get('width_pct_rank')}, ATR streak "
            f"{cand.get('atr_streak_len')}, RSI {cand.get('rsi')}, vol ratio "
            f"{cand.get('vol_ratio')})"),
           ("RANKED watchlist, not a gated signal: top-5 by compression score "
            "over the anti-junk universe (M&A hard-excluded). Levels exist so "
            "the resolver can score next-week outcomes — they are NOT orders "
            "and nothing is queued.")]
    await db.execute(_t("""
        INSERT INTO email_signals_history
          (picked_at, ticker, asset_type, direction, entry, stop, target,
           gap_pct, rel_vol, today_vol, score, catalyst_reason, quality_reasons,
           stop_reason, target_reason, matched_strategy, shadow, rr,
           projected_move_pct, why_selected, instrument_type, source, detection_ts)
        VALUES (NOW(), :tk, 'stocks', 'long', :en, :st, :tg, :gp, :rv, :tv, :sc,
                :cr, :qr, :sr, :tr, :ms, true, :rr, :pm, :wy, 'watch_only',
                :src, :dts)
    """), {
        "tk": tk, "en": lv["entry"], "st": lv["stop"], "tg": lv["target"],
        "gp": float(cand.get("last_chg_pct") or 0.0),
        "rv": float(cand.get("vol_ratio") or 0.0),
        "tv": int(float(cand.get("friday_vol") or 0)),
        "sc": float(cand.get("score") or 0.0),
        "cr": "Saro Sunday watchlist — ranked compression coil (watch-only forward-test)",
        "qr": json.dumps(quality), "sr": lv["stop_reason"], "tr": lv["target_reason"],
        "ms": MATCHED_STRATEGY, "rr": lv["rr"],
        "pm": round((lv["target"] - lv["entry"]) / lv["entry"] * 100.0, 2) if lv["entry"] else 0,
        "wy": json.dumps(why), "src": SOURCE_TAG, "dts": detection_ts,
    })
    await db.commit()
    logger.info(f"[sunwatch] persisted {tk} entry={lv['entry']} "
                f"stop={lv['stop']} target={lv['target']} "
                f"score={cand.get('score')} (shadow=true, watch_only)")
    return True


# ── the email (local template ON PURPOSE — see module docstring) ───────────
def render_watchlist_html(date_key: str, watch: list[dict]) -> str:
    """Compact per-name cards: ticker, Friday close, coil score, criteria
    chips, catalyst note. NO entry/stop/target, NO Confirm/Skip links —
    watch-only honesty. Inline styles per house email conventions."""
    cards = []
    for i, c in enumerate(watch, 1):
        chips = "".join(
            f'<span style="display:inline-block;background:#ede9fe;color:#5b21b6;'
            f'border-radius:999px;padding:2px 9px;font-size:11px;margin:2px 4px 2px 0;">{ch}</span>'
            for ch in (c.get("criteria") or ["ranked by compression score only"]))
        cat = ""
        if c.get("catalyst"):
            cat = (f'<div style="font-size:12px;color:#64748b;margin-top:6px;">'
                   f'&#128240; {_html.escape(str(c["catalyst"])[:140])}</div>')
        cards.append(
            f'<div style="border:1px solid #e2e8f0;border-radius:10px;'
            f'padding:12px 14px;margin:10px 0;">'
            f'<div style="font-size:15px;color:#0f172a;"><b>#{i} {c["ticker"]}</b>'
            f'<span style="color:#475569;font-size:13px;"> &middot; Fri close '
            f'${float(c["close"]):,.2f}</span>'
            f'<span style="float:right;background:#7c3aed;color:#fff;'
            f'border-radius:6px;padding:2px 8px;font-size:12px;">coil score '
            f'{float(c["score"]):.0f}/100</span></div>'
            f'<div style="margin-top:7px;">{chips}</div>{cat}</div>')
    n = len(watch)
    w = WEIGHTS
    return (
        f'<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
        f'sans-serif;max-width:560px;margin:0 auto;padding:18px;">'
        f'<div style="text-align:center;padding:8px 0 14px;">'
        f'<span style="font-size:26px;font-weight:900;letter-spacing:0.14em;'
        f'color:#7c3aed;">SARO</span>'
        f'<span style="font-size:26px;font-weight:300;letter-spacing:0.14em;'
        f'color:#a78bfa;">&nbsp;WEEKLY&nbsp;WATCH</span></div>'
        f'<h2 style="color:#7c3aed;margin:0 0 8px;">&#128302; {n} coiled '
        f'name{"s" if n != 1 else ""} for the week</h2>'
        f'<p style="font-size:13px;color:#334155;margin:0 0 12px;">Sunday scan '
        f'of {date_key}: the most compressed charts in the liquid NASDAQ/NYSE '
        f'universe, ranked by squeeze depth on Friday&#39;s completed daily '
        f'bars. Coils this tight usually resolve with a fast move &mdash; '
        f'direction unknown. Watch for the break; do not front-run it.</p>'
        f'<div style="background:#fef3c7;border:1px solid #f59e0b;'
        f'border-radius:8px;padding:10px 12px;font-size:12.5px;color:#92400e;">'
        f'<b>{NOT_A_SIGNAL_LINE}</b> These are watch candidates ranked by a '
        f'compression score, not confirmed setups. The scanner records each '
        f'name at Friday&#39;s close and grades next week&#39;s outcome '
        f'automatically, so this list is scored against reality.</div>'
        + "".join(cards) +
        f'<p style="font-size:11px;color:#94a3b8;margin-top:10px;">Coil score = '
        f'{w["bb_width"]:.0%} BB-width tightness (percentile rank in its 60-day '
        f'range) + {w["atr_streak"]:.0%} ATR decline streak + '
        f'{w["vol_contraction"]:.0%} volume contraction vs 20-day average + '
        f'{w["rsi_neutral"]:.0%} RSI neutrality + {w["float_tight"]:.0%} float '
        f'tightness (share-count proxy). Chips show which strict Saro 1.3 coil '
        f'criteria each name already meets. Confirmed M&amp;A targets are '
        f'excluded.</p>'
        f'<div style="border-top:1px solid #e2e8f0;margin-top:14px;'
        f'padding-top:10px;font-size:10.5px;color:#94a3b8;">'
        f'<strong style="color:#64748b;">Disclosure.</strong> This watchlist '
        f'reflects automated screening within the proprietary research of '
        f'<strong>Theta Algos LLC</strong> and is for informational purposes '
        f'only. It is not investment advice, a recommendation, or a '
        f'solicitation. Theta Algos LLC is not a registered investment '
        f'adviser, broker-dealer, commodity trading advisor, or commodity '
        f'pool operator. Trading involves substantial risk of loss.</div>'
        f'</div>')


async def _send_watchlist_email(watch: list[dict], date_key: str) -> int:
    """ONE watch-only email per SARO-activated recipient. Recipient gate =
    app.core.saro.SARO_RECIPIENT_WHERE — the exact same
    tier + is_active + saro_signals_enabled gate as the daily pick (currently
    resolves to jaceford12 + ryan.icloud in prod). Returns sends that
    succeeded. Never raises."""
    # LAZY import — keeps the module import graph email-free; only the
    # Sunday send path touches app.services.email. _send is killswitch-aware
    # (subject contains "Saro" -> passes) and sync — calling it directly from
    # async context follows the _send_no_pick_emails precedent.
    from app.services.email import _send
    from app.database import async_session_factory
    from app.core.saro import ensure_saro_column, SARO_RECIPIENT_WHERE
    from sqlalchemy import text as _t
    try:
        async with async_session_factory() as db:
            await ensure_saro_column(db)
            rows = (await db.execute(_t(
                f"SELECT DISTINCT u.email FROM users u WHERE {SARO_RECIPIENT_WHERE}"
            ))).fetchall()
    except Exception as e:
        logger.error(f"[sunwatch] recipient query failed: {e}")
        return 0
    if not rows:
        logger.info("[sunwatch] no SARO-activated recipients — nothing to send")
        return 0
    subject = SUBJECT_FMT.format(n=len(watch), plural=("" if len(watch) == 1 else "s"))
    html = render_watchlist_html(date_key, watch)
    sent = 0
    for row in rows:
        try:
            if _send(row[0], subject, html):
                sent += 1
        except Exception as e:
            logger.error(f"[sunwatch] email to {row[0]} failed: {e}")
    logger.info(f"[sunwatch] watchlist email sent to {sent}/{len(rows)} recipients")
    return sent


# ── Sunday entrypoint ──────────────────────────────────────────────────────
async def run_sunday_watchlist(*, redis_client=None, _now_et_fn=None) -> dict:
    """One Sunday run: latch -> ranked screen -> shadow rows -> email.
    Rows are persisted BEFORE the email (they are the verification backbone;
    a failed send must not cost the week's forward-test data). Never raises."""
    r = redis_client
    own_r = r is None
    try:
        if os.environ.get(ENV_GATE, "1") != "1":
            return {"status": "disabled"}
        from app.engines.options.ignition_shadow import _now_et
        et = (_now_et_fn or _now_et)()
        if et.weekday() != 6:  # SUNDAY ONLY — inverts the weekday cohorts
            return {"status": "not-sunday"}
        date_key = et.strftime("%Y-%m-%d")
        if r is None:
            import redis.asyncio as _redis
            r = _redis.from_url(os.environ.get(C.REDIS_URL_ENV, C.REDIS_URL_DEFAULT),
                                decode_responses=True)
        try:
            got = await r.set(LATCH_FMT.format(date=date_key), "running",
                              ex=C.LATCH_TTL_S, nx=True)
        except Exception as e:
            logger.warning(f"[sunwatch] redis latch failed ({e}) — skip (no dup risk)")
            return {"status": "no-redis"}
        if not got:
            return {"status": "latched"}
        logger.info(f"[sunwatch] {date_key} start "
                    f"(WATCH ONLY — email + shadow rows, never entries)")

        budget: dict = {"eod_pulls": 0, "news_checks": 0, "cap_hit": False}
        watch = await build_watchlist(r, date_key, budget)
        if not watch:
            logger.info(f"[sunwatch] {date_key}: no rankable names — "
                        f"no email, no rows")
            return {"status": "no-candidates"}

        from app.database import async_session_factory
        rows_written = 0
        detection_ts = datetime.now(timezone.utc)
        for cand in watch:
            try:  # fail-open per candidate — one bad row must not kill the run
                async with async_session_factory() as db:
                    await _ensure_saro13_columns(db)  # source/detection_ts STILL
                    # missing in prod (2026-08-18) — ensure before EVERY insert
                    if await persist_watch_row(db, cand, detection_ts):
                        rows_written += 1
            except Exception as e:
                logger.error(f"[sunwatch] persist {cand.get('ticker')} failed: {e}")

        sent = 0
        try:
            sent = await _send_watchlist_email(watch, date_key)
        except Exception as e:  # belt & braces — sender already swallows
            logger.error(f"[sunwatch] email send failed: {e}")

        summary = {"status": "done",
                   "tickers": [c["ticker"] for c in watch],
                   "rows": rows_written, "emails": sent,
                   "fmp_calls_max": 1 + budget["eod_pulls"] + 1 + budget["news_checks"]}
        logger.info(f"[sunwatch] {date_key} done: {summary}")
        return summary
    except Exception as e:  # fail-open — a watch cohort must never break prod
        logger.error(f"[sunwatch] run failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if own_r and r is not None:
            try:
                await r.aclose()
            except Exception:
                pass


# ── scheduler hook — sync, spawns its own RETAINED task, never blocks ──────
_spawned_dates: set[str] = set()


def maybe_spawn_sunday_watchlist(*, _now_et_fn=None) -> bool:
    """Called from the premarket fast loop each 60s tick (the loop runs 24/7,
    so ALL gating lives here — mirrors maybe_spawn_saro13_shadow, with the
    weekday check INVERTED to Sundays only). Env gate, weekday()==6,
    18:00:00-19:30:00 ET window, in-process date set, then a task via
    shadow._spawn_retained (GC-proof — the 2026-08-07 naked-create_task
    incident). The task holds its own redis SETNX latch across restarts.
    Returns True only when a task was spawned. Never raises."""
    try:
        if os.environ.get(ENV_GATE, "1") != "1":
            return False
        from app.engines.options.ignition_shadow import _now_et, _secs
        et = (_now_et_fn or _now_et)()
        if et.weekday() != 6:  # Sundays ONLY
            return False
        s = _secs(et)
        if not (SPAWN_START_S <= s < SPAWN_END_S):
            return False
        date_key = et.strftime("%Y-%m-%d")
        if date_key in _spawned_dates:
            return False
        _spawned_dates.add(date_key)

        async def _task():
            try:
                res = await run_sunday_watchlist()
                logger.info(f"[sunwatch] Sunday task finished: {res.get('status')}")
            except Exception as e:  # belt & braces — run already swallows
                logger.error(f"[sunwatch] task crashed: {e}")

        _spawn_retained(_task())
        logger.info(f"[sunwatch] spawned Sunday watchlist task for {date_key}")
        return True
    except Exception as e:
        logger.warning(f"[sunwatch] spawn check failed: {e}")
        return False
