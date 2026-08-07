"""Saro 1.3 SHADOW runner — one run per trading morning, rows only.

SHADOW ONLY — ABSOLUTE: this module NEVER sends emails, NEVER queues paper
or live entries, and NEVER imports pick_router / emit_theta_pick /
app.services.email / any broker order path (tests/test_saro13.py asserts
this on the import graph AND by source-token scan). The only side effects:
  * shadow rows in email_signals_history: shadow=true,
    source='saro13_shadow', matched_strategy='saro13:compression_coil',
    asset_type='stocks', instrument_type='watch_only'
  * nightly ATM-IV rows in saro_iv_history (iv_snapshot.py)
  * per-day redis caches/latches (theta:saro13:*, saro13:*)
The existing outcome resolver scores the rows against real daily candles
automatically, so Saro 1.3 accumulates forward-test stats for free.

Flow (weekdays, spawned 09:33-11:00 ET by the premarket fast loop):
  1. redis SETNX latch theta:saro13:{ET-date} — no latch -> no run.
  2. S1+S2+S3(M&A) screen (screen.run_screen — <= 42 FMP calls/day worst
     case, redis-cached per ET day; compression math on COMPLETED daily
     sessions only, live partial excluded).
  3. S3 IV-Rank gate per candidate (iv_snapshot.iv_rank_gate — fail-OPEN
     with a loud 'iv-history-warming' warning until 20 sessions of
     accumulated ATM-IV history exist).
  4. ONE shadow row per survivor. Levels (NOT NULL columns — a failed
     INSERT after the latch would forfeit the day):
       entry  = last COMPLETED daily close (the coil's resting price)
       stop   = lower Bollinger band (daily 20,2 — the coil floor), else
                the last swing low (min low, 10 sessions), else -3%
       target = entry + 2x compression range, where compression range =
                upperBB - lowerBB (the coil width): the measured-move
                convention — a breakout from an N-point coil projects ~2N.
     build_levels ALWAYS returns three positive floats stop < entry < target.

S4 DELIVERY — SHADOW REALITY (honest limits): a shadow track never alerts.
  (a) detection_ts is stamped on every row (UTC, microsecond precision —
      milliseconds are measurable) at the moment the ticker clears the last
      gate, so end-to-end alert latency is MEASURABLE for the readout.
  (b) the instant-alert stub is env-gated: SARO13_ALERT_WEBHOOK (default
      empty = OFF) would POST the row JSON the moment a ticker passes; an
      APNS push hook is sketched in comments (APNS_ENABLED still 0).
  HONESTY: the data cadence is 1-min/daily bars plus a 60s spawn loop, and
  the compression pass runs once per morning — detection fires within
  ~1-2 minutes of the triggering bar/window, NOT in literal milliseconds;
  the ms-precision stamp exists to measure that gap, not to promise it away.

Execution/exits are NEXT-PHASE, deliberately unbuilt: signal quality first
(house doctrine). Nothing here may be repointed at the live path.
"""
from __future__ import annotations

import asyncio
import json
from loguru import logger  # stdlib INFO invisible in prod
import os
from datetime import datetime, timezone

from app.engines.scanner.saro13 import config as C

# (loguru imported above)

# email_signals_history is missing BOTH of these in prod (the `source` ALTER
# from saro12 never ran — its maiden run was GC-killed 2026-08-07 before
# _ensure_saro12_columns fired). Run _ensure_saro13_columns before EVERY
# INSERT: base shadow cols first, then these, idempotent ALTERs.
_SARO13_COLS = ["source text", "detection_ts timestamptz"]


async def _ensure_saro13_columns(db):
    from sqlalchemy import text as _t
    # base shadow cols first (matched_strategy/shadow/rr/... — shared helper)
    from app.engines.scanner.shadow import _ensure_columns as _base
    await _base(db)
    for col in _SARO13_COLS:
        await db.execute(_t(f"ALTER TABLE email_signals_history ADD COLUMN IF NOT EXISTS {col}"))
    await db.commit()


# ── row construction ───────────────────────────────────────────────────────
def build_levels(cand: dict) -> dict:
    """entry/stop/target per the spec derivation (see module docstring).
    ALWAYS returns three positive floats with stop < entry < target — the
    columns are NOT NULL and a failed INSERT would kill the day's run after
    the latch is taken."""
    entry = float(cand.get("close") or cand.get("price") or 0.0)
    if entry <= 0:
        entry = max(float(cand.get("price") or 0.0), 0.01)
    stop = stop_reason = None
    for val, why in ((cand.get("lower_bb"),
                      "lower Bollinger band (daily 20,2) — the coil floor"),
                     (cand.get("swing_low"),
                      f"last swing low (min low, {C.SWING_LOW_LOOKBACK} sessions)")):
        try:
            if val is not None and 0 < float(val) < entry:
                stop, stop_reason = float(val), why
                break
        except Exception:
            continue
    if stop is None:
        stop = entry * (1.0 - C.FALLBACK_STOP_PCT / 100.0)
        stop_reason = f"fallback -{C.FALLBACK_STOP_PCT:g}% (no BB/swing stop below entry)"
    risk = entry - stop
    rng = 0.0
    try:
        rng = float(cand.get("compression_range") or 0.0)
    except Exception:
        pass
    if rng > 0:
        target = entry + C.TARGET_RANGE_MULT * rng
        target_reason = (f"entry + {C.TARGET_RANGE_MULT:g}x compression range "
                         f"({rng:.4g} = upperBB - lowerBB coil width, measured move)")
    else:
        target = entry + 2.0 * risk
        target_reason = "fallback 2R (degenerate compression range)"
    if target <= entry:  # belt & braces — rng>0 and risk>0 already imply this
        target = entry + 2.0 * risk
        target_reason = "fallback 2R (guard)"
    rr = round((target - entry) / risk, 2) if risk > 0 else C.TARGET_RANGE_MULT
    return {"entry": round(entry, 4), "stop": round(stop, 4),
            "target": round(target, 4), "rr": rr,
            "stop_reason": stop_reason, "target_reason": target_reason}


async def persist_shadow_row(db, cand: dict, detection_ts: datetime) -> bool:
    """INSERT one saro13 shadow row (column list mirrors the saro12 shadow
    INSERT + detection_ts). Dedup: one (ticker, saro13:compression_coil,
    ET-day) row. Returns True when written."""
    from sqlalchemy import text as _t
    tk = cand["ticker"]
    dup = (await db.execute(_t(
        "SELECT 1 FROM email_signals_history WHERE ticker=:tk AND matched_strategy=:ms "
        "AND picked_at::date = (NOW() AT TIME ZONE 'America/New_York')::date LIMIT 1"
    ), {"tk": tk, "ms": C.MATCHED_STRATEGY})).first()
    if dup:
        logger.info(f"[saro13-shadow] {tk} already recorded today — skip")
        return False
    lv = build_levels(cand)
    quality = list(cand.get("screen_reasons") or [])
    quality.append("predictive consolidation: S1 compression + S2 guardrails "
                   "+ S3 theta viability (see reasons above)")
    why = [f"Saro 1.3 shadow: compression coil found BEFORE the move "
           f"(BB width {cand.get('bb_width')}, RSI {cand.get('rsi')}, "
           f"vol ratio {cand.get('vol_ratio')})",
           f"S1: width in lowest {C.BBWIDTH_LOWEST_PCT:g}% of "
           f"{C.BBWIDTH_RANGE_SESSIONS}d, ATR down {C.ATR_DECLINE_SESSIONS} "
           f"sessions, day change <= +{C.DAILY_CHANGE_MAX_PCT:g}%; "
           f"S2: RSI [{C.RSI_BAND_LO:g},{C.RSI_BAND_HI:g}], volume < SMA"
           f"{C.VOL_SMA_PERIOD}; S3: IV-Rank + M&A exclude"]
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
        "gp": float(cand.get("live_chg_pct") or 0.0),
        "rv": float(cand.get("vol_ratio") or 0.0),
        "tv": int(cand.get("today_vol") or 0),
        "sc": round((lv["target"] - lv["entry"]) / lv["entry"] * 100.0, 2) if lv["entry"] else 0,
        "cr": "saro13 predictive consolidation — compression coil (shadow forward-test)",
        "qr": json.dumps(quality), "sr": lv["stop_reason"], "tr": lv["target_reason"],
        "ms": C.MATCHED_STRATEGY, "rr": lv["rr"],
        "pm": round((lv["target"] - lv["entry"]) / lv["entry"] * 100.0, 2) if lv["entry"] else 0,
        "wy": json.dumps(why), "src": C.SOURCE_TAG, "dts": detection_ts,
    })
    await db.commit()
    logger.info(f"[saro13-shadow] persisted {tk} entry={lv['entry']} "
                f"stop={lv['stop']} target={lv['target']} "
                f"detection_ts={detection_ts.isoformat()} (shadow=true)")
    return True


# ── S4 instant-alert stub (flag-gated, OFF by default — shadow honesty) ────
async def _fire_instant_alert(row: dict) -> str:
    """POST the row JSON to SARO13_ALERT_WEBHOOK the moment a ticker passes.
    Default (env empty/unset) = OFF: returns 'off' without any I/O. Never
    raises; failures are logged and swallowed. This stub must NEVER touch
    email paths — it exists so flipping ONE env var turns on instant
    delivery without new code.

    # APNS PUSH HOOK (NOT BUILT — APNS_ENABLED still 0). When mobile push
    # lands, mirror the webhook gate here:
    #   if os.environ.get(C.APNS_ENABLED_ENV, "0") == "1":
    #       await apns_push(device_tokens,
    #                       title=f"Saro 1.3: {row['ticker']} coiled",
    #                       body=f"entry {row['entry']} / watch-only shadow")
    """
    url = (os.environ.get(C.ALERT_WEBHOOK_ENV) or "").strip()
    if not url:
        return "off"
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=5.0)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=row) as resp:
                logger.info(f"[saro13-alert] webhook POST {resp.status} "
                            f"for {row.get('ticker')}")
                return f"posted:{resp.status}"
    except Exception as e:
        logger.warning(f"[saro13-alert] webhook failed (non-fatal): {e}")
        return "error"


# ── daily entrypoint ───────────────────────────────────────────────────────
async def run_saro13_shadow_scan(*, redis_client=None) -> dict:
    """One daily shadow run: latch -> screen -> IV gate -> rows. Never raises."""
    r = redis_client
    own_r = r is None
    try:
        if os.environ.get(C.ENV_GATE, "1") != "1":
            return {"status": "disabled"}
        from app.engines.options.ignition_shadow import _now_et
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
            logger.warning(f"[saro13-shadow] redis latch failed ({e}) — skip (no dup risk)")
            return {"status": "no-redis"}
        if not got:
            return {"status": "latched"}
        logger.info(f"[saro13-shadow] {date_key} start (SHADOW ONLY — rows, no emails/entries)")

        from app.engines.scanner.saro13.screen import run_screen
        from app.engines.scanner.saro13.iv_snapshot import iv_rank_gate
        cands = await run_screen(r=r, date_key=date_key)
        if not cands:
            logger.info(f"[saro13-shadow] no candidates for {date_key} — exit")
            return {"status": "no-candidates"}

        from app.database import async_session_factory
        fired: list[str] = []
        for cand in cands:
            try:  # fail-open per candidate — one bad row must not kill the day
                # S4: detection stamp the moment the last gate is evaluated
                # (UTC, microsecond precision — latency is measurable).
                detection_ts = datetime.now(timezone.utc)
                wrote = False
                async with async_session_factory() as db:
                    await _ensure_saro13_columns(db)
                    iv_ok, iv_note = await iv_rank_gate(db, cand["ticker"])
                    if not iv_ok:
                        logger.info(f"[saro13-shadow] {cand['ticker']} IV reject: {iv_note}")
                        continue
                    cand.setdefault("screen_reasons", []).append(iv_note)
                    wrote = await persist_shadow_row(db, cand, detection_ts)
                if wrote:
                    fired.append(cand["ticker"])
                    lv = build_levels(cand)
                    await _fire_instant_alert({
                        "ticker": cand["ticker"], "source": C.SOURCE_TAG,
                        "matched_strategy": C.MATCHED_STRATEGY,
                        "detection_ts": detection_ts.isoformat(),
                        "entry": lv["entry"], "stop": lv["stop"],
                        "target": lv["target"],
                        "note": "SHADOW — watch only, no execution"})
            except Exception as e:
                logger.error(f"[saro13-shadow] {cand.get('ticker')} eval failed: {e}")

        summary = {"status": "done", "candidates": len(cands),
                   "fired": len(fired), "fired_tickers": sorted(fired)}
        logger.info(f"[saro13-shadow] {date_key} done: {summary}")
        return summary
    except Exception as e:  # fail-open — a shadow cohort must never break prod
        logger.error(f"[saro13-shadow] run failed: {e}")
        return {"status": "error", "error": str(e)}
    finally:
        if own_r and r is not None:
            try:
                await r.aclose()
            except Exception:
                pass


# ── scheduler hook — sync, spawns its own task, never blocks the loop ─────
_spawned_scan_dates: set[str] = set()
_spawned_iv_dates: set[str] = set()


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


def maybe_spawn_saro13_shadow(*, _now_et_fn=None) -> bool:
    """Called from the premarket fast loop each 60s tick (the loop runs 24/7
    with no window gate of its own, so ALL gating lives here). Mirrors
    saro12.shadow.maybe_spawn_saro12_shadow: env gate, weekday check, ET
    window check, in-process date set, then a RETAINED asyncio task — the
    task holds its own redis SETNX latch (belt & braces across restarts).
    Dispatches BOTH saro13 jobs so the scheduler needs exactly one hook:
      09:33-11:00 ET -> daily shadow scan    (latch theta:saro13:{date})
      18:30-21:00 ET -> nightly IV snapshot  (latch theta:saro13:iv_done:{date})
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
                    res = await run_saro13_shadow_scan()
                    logger.info(f"[saro13-shadow] daily task finished: {res.get('status')}")
                except Exception as e:  # belt & braces — run already swallows
                    logger.error(f"[saro13-shadow] task crashed: {e}")

            _spawn_retained(_scan_task())
            logger.info(f"[saro13-shadow] spawned daily scan task for {date_key}")
            return True

        if C.IV_SPAWN_START_S <= s < C.IV_SPAWN_END_S and date_key not in _spawned_iv_dates:
            _spawned_iv_dates.add(date_key)

            async def _iv_task():
                try:
                    from app.engines.scanner.saro13.iv_snapshot import run_iv_snapshot_once
                    res = await run_iv_snapshot_once()
                    logger.info(f"[saro13-iv] nightly task finished: {res.get('status')}")
                except Exception as e:
                    logger.error(f"[saro13-iv] task crashed: {e}")

            _spawn_retained(_iv_task())
            logger.info(f"[saro13-iv] spawned nightly IV snapshot task for {date_key}")
            return True
        return False
    except Exception as e:
        logger.warning(f"[saro13-shadow] spawn check failed: {e}")
        return False


# ── RUNBOOK: 3-way readout — saro 1.0 vs saro12 vs saro13 (same window) ────
# The outcome resolver scores all three cohorts identically (target hit =
# win, stop hit = loss, 5-day expire). Through ~2026-08-20, run on edge:
#
#   docker exec edge_db psql -U edge_user -d edge_db -c "
#     SELECT CASE WHEN source='saro13_shadow'
#                      OR matched_strategy LIKE 'saro13:%' THEN 'saro13-shadow'
#                 WHEN source='saro12_shadow'
#                      OR matched_strategy LIKE 'saro12:%' THEN 'saro12-shadow'
#                 ELSE 'saro10-live' END                    AS cohort,
#            COUNT(*)                                       AS signals,
#            COUNT(*) FILTER (WHERE outcome='win')          AS wins,
#            COUNT(*) FILTER (WHERE outcome='loss')         AS losses,
#            ROUND(AVG(outcome_pct)::numeric, 2)            AS avg_outcome_pct,
#            ROUND((COUNT(*) FILTER (WHERE outcome='win'))::numeric
#                  / NULLIF(COUNT(*) FILTER (WHERE outcome IN ('win','loss')),0)
#                  * 100, 1)                                AS win_rate_pct
#     FROM email_signals_history
#     WHERE picked_at >= '2026-08-06' AND picked_at < '2026-08-21'
#       AND (source IN ('saro12_shadow','saro13_shadow')
#            OR matched_strategy LIKE 'saro12:%' OR matched_strategy LIKE 'saro13:%'
#            OR (asset_type='options' AND shadow IS NOT TRUE))
#     GROUP BY 1 ORDER BY 1;"
#
# saro13 detection-latency drill-down (S4 readout — how instant is "instant"):
#   ... SELECT ticker, picked_at, detection_ts,
#              EXTRACT(EPOCH FROM (picked_at - detection_ts)) AS persist_lag_s
#       FROM email_signals_history WHERE source='saro13_shadow'
#       ORDER BY picked_at DESC;
# Interpretation doctrine: >= 30 resolved rows per cohort before any
# promotion talk (the 30-sample gate); compare win_rate AND avg_outcome_pct
# — a higher win rate with worse expectancy is not a pass. Early saro13
# rows are IV-Rank UNGATED (iv-history-warming) — split the readout on
# quality_reasons LIKE '%iv-history-warming%' before drawing conclusions.
