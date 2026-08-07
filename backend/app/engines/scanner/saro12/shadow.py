"""Saro 1.2 SHADOW runner — one run per trading morning, rows only.

SHADOW ONLY — ABSOLUTE: this module NEVER sends emails, NEVER queues paper
or live entries, and NEVER imports pick_router / emit_theta_pick /
app.services.email / any broker order path (tests/test_saro12.py asserts
this on the import graph AND by source-token scan). The only side effects
are:
  * shadow rows in email_signals_history: shadow=true,
    source='saro12_shadow', matched_strategy='saro12:squeeze_breakout',
    asset_type='stocks', instrument_type='watch_only'
  * per-day redis caches/latches (theta:saro12:*, saro12:*)
The existing outcome resolver (scanner._resolve_email_signal_outcomes)
scores the rows against real daily candles automatically (target hit=win /
stop hit=loss / 5-day expire), so Saro 1.2 accumulates forward-test stats
for free.

Flow (weekdays, spawned 09:33-11:00 ET by the premarket fast loop):
  1. redis SETNX latch theta:saro12:{ET-date} — no latch -> no run
     (prevents duplicate daily cohorts across restarts/workers).
  2. Section-1 screen (screen.run_screen — bulk FMP screener, anti-junk,
     RVOL cap; ~76 FMP calls/day worst case, redis-cached per ET day).
  3. Section-2 trigger per candidate (indicators.alert_state): TTM squeeze
     active + RVOL >= 4 + close breaks the upper Keltner -> ONE shadow row,
     entry/stop/target from the alert bar (structure levels when available,
     spec-derived fallback otherwise — entry/stop/target are NOT NULL).
  4. Optional bounded re-check loop (env SARO12_RECHECK_ENABLED, default
     OFF) re-evaluates the top RECHECK_TOP_N un-alerted candidates every
     RECHECK_SECONDS until 11:00 ET. Left off to hold the 76-call budget.

Sections 4-5 of the spec (execution + exits) are NEXT-PHASE, deliberately
unbuilt: signal quality first (house doctrine after the June/July
incidents). When the 2-week review passes, execution wiring gets its own
review — nothing here may be repointed at the live path.
"""
from __future__ import annotations

import asyncio
import json
from loguru import logger  # stdlib INFO was invisible in prod (2026-08-07)
import os
from typing import Optional

from app.engines.scanner.saro12 import config as C

# (loguru imported above — stdlib getLogger dropped INFO below root level)

# `source` column: email_signals_history has no such column in prod — add it
# with the established idempotent ALTER pattern (scanner/shadow._SHADOW_COLS).
_SARO12_COLS = ["source text"]


async def _ensure_saro12_columns(db):
    from sqlalchemy import text as _t
    # base shadow cols first (matched_strategy/shadow/rr/... — shared helper)
    from app.engines.scanner.shadow import _ensure_columns as _base
    await _base(db)
    for col in _SARO12_COLS:
        await db.execute(_t(f"ALTER TABLE email_signals_history ADD COLUMN IF NOT EXISTS {col}"))
    await db.commit()


# ── row construction ───────────────────────────────────────────────────────
def build_levels(cand: dict, alert: dict) -> dict:
    """entry/stop/target from the alert bar. Tries the shared structure-level
    engine first; falls back to spec-derived levels (stop = lower Keltner or
    alert-bar low, target = entry + TARGET_RR * risk). ALWAYS returns three
    positive floats with stop < entry < target — the columns are NOT NULL
    and a failed INSERT would kill the day's run after the latch is taken."""
    entry = float(alert.get("close") or cand.get("price") or 0.0)
    if entry <= 0:
        entry = max(float(cand.get("price") or 0.0), 0.01)
    stop = None
    stop_reason = target_reason = None
    if cand.get("bars"):  # structure engine only with real bars (deterministic fallback otherwise)
        try:
            from app.engines.scanner.levels import compute_levels
            lv = compute_levels("long", entry, bars=cand.get("bars"), rr=C.TARGET_RR)
            if lv is not None and getattr(lv, "ok", False) and 0 < lv.stop < entry < lv.target:
                return {"entry": round(entry, 4), "stop": round(float(lv.stop), 4),
                        "target": round(float(lv.target), 4),
                        "rr": round(float(lv.rr), 2),
                        "stop_reason": lv.stop_reason, "target_reason": lv.target_reason}
        except Exception as e:
            logger.debug(f"[saro12-shadow] compute_levels fallback for {cand.get('ticker')}: {e}")
    # spec-derived fallback: lower Keltner, then alert-bar low, then -3%
    for val, why in ((alert.get("kc_lower"), "lower Keltner line (squeeze channel)"),
                     (alert.get("bar_low"), "alert-bar low")):
        try:
            if val is not None and 0 < float(val) < entry:
                stop, stop_reason = float(val), why
                break
        except Exception:
            continue
    if stop is None:
        stop = entry * (1.0 - C.FALLBACK_STOP_PCT / 100.0)
        stop_reason = f"fallback -{C.FALLBACK_STOP_PCT:g}% (no structure/KC stop below entry)"
    risk = entry - stop
    target = entry + C.TARGET_RR * risk
    target_reason = f"{C.TARGET_RR:g}R measured move above KC breakout entry"
    return {"entry": round(entry, 4), "stop": round(stop, 4),
            "target": round(target, 4), "rr": C.TARGET_RR,
            "stop_reason": stop_reason, "target_reason": target_reason}


async def persist_shadow_row(db, cand: dict, alert: dict,
                             oi_surge_pct: Optional[float]) -> bool:
    """INSERT one saro12 shadow row (column list mirrors the ignition/V2
    shadow INSERTs + the source column). Dedup: one (ticker,
    saro12:squeeze_breakout, ET-day) row. Returns True when written."""
    from sqlalchemy import text as _t
    tk = cand["ticker"]
    dup = (await db.execute(_t(
        "SELECT 1 FROM email_signals_history WHERE ticker=:tk AND matched_strategy=:ms "
        "AND picked_at::date = (NOW() AT TIME ZONE 'America/New_York')::date LIMIT 1"
    ), {"tk": tk, "ms": C.MATCHED_STRATEGY})).first()
    if dup:
        logger.info(f"[saro12-shadow] {tk} already recorded today — skip")
        return False
    lv = build_levels(cand, alert)
    quality = list(cand.get("screen_reasons") or [])
    quality.append("TTM squeeze (BB(20,2) inside KC(20,1.5xATR)) -> upper-KC breakout")
    if oi_surge_pct is not None:
        from app.engines.scanner.saro12.oi_snapshot import is_oi_surge
        tag = "SURGE" if is_oi_surge(oi_surge_pct) else "no surge"
        quality.append(f"OTM-call OI 24h change {oi_surge_pct:+.1f}% ({tag})")
    why = [f"Saro 1.2 shadow: squeeze breakout, RVOL {cand.get('rvol')}x",
           f"screen: float<{C.FLOAT_MAX_SHARES:,}, ${C.PRICE_MIN:g}-{C.PRICE_MAX:g}, "
           f"RVOL>={C.RVOL_MIN:g}, RSI/MACD momentum, anti-junk"]
    await db.execute(_t("""
        INSERT INTO email_signals_history
          (picked_at, ticker, asset_type, direction, entry, stop, target,
           gap_pct, rel_vol, today_vol, score, catalyst_reason, quality_reasons,
           stop_reason, target_reason, matched_strategy, shadow, rr,
           projected_move_pct, why_selected, instrument_type, source)
        VALUES (NOW(), :tk, 'stocks', 'long', :en, :st, :tg, 0, :rv, :tv, :sc,
                :cr, :qr, :sr, :tr, :ms, true, :rr, :pm, :wy, 'watch_only', :src)
    """), {
        "tk": tk, "en": lv["entry"], "st": lv["stop"], "tg": lv["target"],
        "rv": float(cand.get("rvol") or 0), "tv": int(cand.get("today_vol") or 0),
        "sc": float(cand.get("rvol") or 0),
        "cr": "saro12 TTM-squeeze breakout (shadow forward-test)",
        "qr": json.dumps(quality), "sr": lv["stop_reason"], "tr": lv["target_reason"],
        "ms": C.MATCHED_STRATEGY, "rr": lv["rr"],
        "pm": round((lv["target"] - lv["entry"]) / lv["entry"] * 100.0, 2) if lv["entry"] else 0,
        "wy": json.dumps(why), "src": C.SOURCE_TAG,
    })
    await db.commit()
    logger.info(f"[saro12-shadow] persisted {tk} entry={lv['entry']} "
                f"stop={lv['stop']} target={lv['target']} (shadow=true)")
    return True


# ── daily entrypoint ───────────────────────────────────────────────────────
async def run_saro12_shadow_scan(*, redis_client=None) -> dict:
    """One daily shadow run: latch -> screen -> alert -> rows. Never raises."""
    r = redis_client
    own_r = r is None
    try:
        if os.environ.get(C.ENV_GATE, "1") != "1":
            return {"status": "disabled"}
        from app.engines.options.ignition_shadow import _now_et, _secs
        et = _now_et()
        if et.weekday() >= 5:
            return {"status": "weekend"}
        date_key = et.strftime("%Y-%m-%d")
        if r is None:
            import redis.asyncio as _redis
            r = _redis.from_url(os.environ.get(C.REDIS_URL_ENV, C.REDIS_URL_DEFAULT),
                                decode_responses=True)
        try:
            got = await r.set(C.LATCH_SCAN_FMT.format(date=date_key), "running",
                              ex=C.LATCH_TTL_S, nx=True)
        except Exception as e:
            logger.warning(f"[saro12-shadow] redis latch failed ({e}) — skip (no dup risk)")
            return {"status": "no-redis"}
        if not got:
            return {"status": "latched"}
        logger.info(f"[saro12-shadow] {date_key} start (SHADOW ONLY — rows, no emails/entries)")

        from app.engines.scanner.saro12.screen import run_screen
        from app.engines.scanner.saro12.indicators import bars_to_df, alert_state
        from app.engines.scanner.saro12.oi_snapshot import otm_call_oi_surge
        cands = await run_screen(r=r, date_key=date_key)
        if not cands:
            logger.info(f"[saro12-shadow] no candidates for {date_key} — exit")
            return {"status": "no-candidates"}

        from app.database import async_session_factory
        fired: list[str] = []

        async def _eval_and_persist(cand) -> bool:
            st = alert_state(bars_to_df(cand.get("bars")), cand.get("rvol"))
            if not (st["alert"] and cand.get("momentum_ok")):
                return False
            surge = await otm_call_oi_surge(cand["ticker"], r=r)
            async with async_session_factory() as db:
                await _ensure_saro12_columns(db)
                return await persist_shadow_row(db, cand, st, surge)

        for cand in cands:
            try:
                if await _eval_and_persist(cand):
                    fired.append(cand["ticker"])
            except Exception as e:  # fail-open per candidate
                logger.error(f"[saro12-shadow] {cand.get('ticker')} eval failed: {e}")

        # optional bounded re-check loop (default OFF — budget discipline)
        if os.environ.get(C.RECHECK_ENV_GATE, "0") == "1":
            from app.engines.data_feeds.fmp_feed import fetch_intraday_bars
            pending = [c for c in cands if c["ticker"] not in fired][:C.RECHECK_TOP_N]
            while pending and _secs(_now_et()) < C.SCAN_SPAWN_END_S:
                await asyncio.sleep(C.RECHECK_SECONDS)
                still = []
                for cand in pending:
                    try:
                        cand["bars"] = await fetch_intraday_bars(cand["ticker"])
                        if await _eval_and_persist(cand):
                            fired.append(cand["ticker"])
                        else:
                            still.append(cand)
                    except Exception as e:
                        logger.error(f"[saro12-shadow] recheck {cand.get('ticker')} failed: {e}")
                pending = still

        summary = {"status": "done", "candidates": len(cands),
                   "fired": len(fired), "fired_tickers": sorted(fired)}
        logger.info(f"[saro12-shadow] {date_key} done: {summary}")
        return summary
    except Exception as e:  # fail-open — a shadow cohort must never break prod
        logger.error(f"[saro12-shadow] run failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if own_r and r is not None:
            try:
                await r.aclose()
            except Exception:
                pass


# ── scheduler hook — sync, spawns its own task, never blocks the loop ─────
_spawned_scan_dates: set[str] = set()
_spawned_oi_dates: set[str] = set()


# GC-PROOF TASK RETENTION (2026-08-07 maiden-run incident): the event loop
# only weak-refs tasks - a naked asyncio.create_task() can be garbage-
# collected mid-flight (silent: latch set, zero logs, no traceback). Same
# bug family as the 7/16 fast-theta-loop death. Every spawn goes through
# _spawn_retained().
_RETAINED: set = set()


def _spawn_retained(coro):
    t = asyncio.create_task(coro)
    _RETAINED.add(t)
    t.add_done_callback(_RETAINED.discard)
    return t


def maybe_spawn_saro12_shadow(*, _now_et_fn=None) -> bool:
    """Called from the premarket fast loop each 60s tick (the loop runs 24/7
    with no window gate of its own, so ALL gating lives here). Mirrors
    ignition_shadow.maybe_spawn_ignition_shadow: env gate, weekday check,
    ET-window check, in-process date set, then asyncio.create_task — the
    task holds its own redis SETNX latch (belt & braces across restarts).
    Dispatches BOTH saro12 jobs so the scheduler needs exactly one hook:
      09:33-11:00 ET -> daily shadow scan   (latch theta:saro12:{date})
      18:30-21:00 ET -> nightly OI snapshot (latch theta:saro12:oi_done:{date})
    Returns True only when a task was spawned. Never raises."""
    try:
        if os.environ.get(C.ENV_GATE, "1") != "1":
            return False
        from app.engines.options.ignition_shadow import _now_et, _secs
        et = (_now_et_fn or _now_et)()
        if et.weekday() >= 5:
            return False
        s = _secs(et)
        date_key = et.strftime("%Y-%m-%d")

        if C.SCAN_SPAWN_START_S <= s < C.SCAN_SPAWN_END_S and date_key not in _spawned_scan_dates:
            _spawned_scan_dates.add(date_key)

            async def _scan_task():
                try:
                    res = await run_saro12_shadow_scan()
                    logger.info(f"[saro12-shadow] daily task finished: {res.get('status')}")
                except Exception as e:  # belt & braces — run already swallows
                    logger.error(f"[saro12-shadow] task crashed: {e}")

            _spawn_retained(_scan_task())
            logger.info(f"[saro12-shadow] spawned daily scan task for {date_key}")
            return True

        if C.OI_SPAWN_START_S <= s < C.OI_SPAWN_END_S and date_key not in _spawned_oi_dates:
            _spawned_oi_dates.add(date_key)

            async def _oi_task():
                try:
                    from app.engines.scanner.saro12.oi_snapshot import run_oi_snapshot_once
                    res = await run_oi_snapshot_once()
                    logger.info(f"[saro12-oi] nightly task finished: {res.get('status')}")
                except Exception as e:
                    logger.error(f"[saro12-oi] task crashed: {e}")

            _spawn_retained(_oi_task())
            logger.info(f"[saro12-oi] spawned nightly OI snapshot task for {date_key}")
            return True
        return False
    except Exception as e:
        logger.warning(f"[saro12-shadow] spawn check failed: {e}")
        return False


# ── RUNBOOK: comparing Saro 1.2 shadow vs Saro 1.0 after 2 weeks ──────────
# The outcome resolver scores both cohorts identically (target hit = win,
# stop hit = loss, 5-day expire). After ~10 trading days, run on edge:
#
#   docker exec edge_db psql -U edge_user -d edge_db -c "
#     SELECT CASE WHEN matched_strategy LIKE 'saro12:%' THEN 'saro12-shadow'
#                 ELSE 'saro10-live' END AS cohort,
#            COUNT(*)                                   AS signals,
#            COUNT(*) FILTER (WHERE outcome='win')      AS wins,
#            COUNT(*) FILTER (WHERE outcome='loss')     AS losses,
#            ROUND(AVG(outcome_pct)::numeric, 2)        AS avg_outcome_pct,
#            ROUND((COUNT(*) FILTER (WHERE outcome='win'))::numeric
#                  / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('win','loss')),0)
#                  * 100, 1)                            AS win_rate_pct
#     FROM email_signals_history
#     WHERE picked_at >= NOW() - INTERVAL '14 days'
#       AND (matched_strategy LIKE 'saro12:%'
#            OR (asset_type='options' AND shadow IS NOT TRUE))
#     GROUP BY 1 ORDER BY 1;"
#
# Row-level drill-down (levels, reasons, OI-surge notes):
#   ... WHERE source='saro12_shadow' ORDER BY picked_at DESC;
# Interpretation doctrine: >= 30 resolved saro12 rows before any promotion
# talk (same 30-sample gate as the shadow templates); compare win_rate AND
# avg_outcome_pct — a higher win rate with worse expectancy is not a pass.
# Promotion/execution (Sections 4-5) is a separate, owner-approved phase.
