"""IGNITION SHADOW scanner — SHADOW ONLY (owner approved 2026-07-14).

Two-week forward measurement of an opening-drive "ignition" entry on the
Track A pre-lock candidates. This module NEVER sends emails, NEVER places
orders, and NEVER routes picks — its only side effect is shadow rows in
email_signals_history (shadow=true, instrument_type='watch_only',
matched_strategy='ignition_shadow'), which the existing shadow resolver
scores automatically.

How it works (weekdays, 09:30:20–09:36:00 ET):
  1. The scheduler spawns ONE asyncio task per day (redis SETNX latch
     theta:ignition:done:{ET-date}); the task loads the Track A pre-lock
     candidate list from redis key theta:ignition:candidates:{ET-date}
     (empty/missing -> log + exit).
  2. For each candidate (max 5) it polls FMP /stable/quote-short every
     ~15s. The OPENING RANGE is the min/max of the quotes observed
     09:30:20–09:31:30 ET. HONEST APPROXIMATION, documented: this FMP plan
     has no premarket/realtime 1-minute bars, so the OR is quote-SAMPLED
     (~5 samples/symbol at 15s cadence), not the true tick-level 1-minute
     high/low. quote-short returns {symbol, price, change, volume} with no
     timestamp, one symbol per request (comma lists return [] on this plan).
  3. From 09:31:30 ET on: the first candidate whose polled price breaks the
     quote-sampled OR-high by >= 0.1% records ONE shadow row (long only):
     entry = break price, stop = OR-low, target = entry + 2R. At most
     IGNITION_MAX_ROWS_PER_DAY (2) rows per day; one row per ticker per day
     (SQL dedup, same rule as the daily shadow scans).

Isolation / safety:
  * Env gate IGNITION_SHADOW_ENABLED (default '1').
  * Runs on its own asyncio task — it can never delay the scheduler loops.
  * Every failure fails OPEN (log + skip); no exception escapes the task.
  * No redis latch acquired -> no run (prevents duplicate daily cohorts).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, time as dtime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# ── constants ──────────────────────────────────────────────────────────────
MAX_CANDIDATES = 5                 # cap on Track A pre-lock candidates polled
IGNITION_MAX_ROWS_PER_DAY = 2      # hard cap on shadow rows recorded per day
POLL_SECONDS = 15.0                # quote-short poll cadence per loop pass
BREAK_PCT = 0.1                    # break trigger: price >= OR-high * (1 + 0.1%)

# window boundaries as seconds-since-midnight ET
OR_START_S = 9 * 3600 + 30 * 60 + 20     # 09:30:20 — first OR sample
OR_END_S = 9 * 3600 + 31 * 60 + 30       # 09:31:30 — OR locks, breaks armed
WINDOW_END_S = 9 * 3600 + 36 * 60        # 09:36:00 — task stops
SPAWN_START_S = 9 * 3600 + 29 * 60       # 09:29:00 — earliest scheduler spawn

MATCHED_STRATEGY = "ignition_shadow"
LATCH_KEY_FMT = "theta:ignition:done:{date}"
CANDIDATES_KEY_FMT = "theta:ignition:candidates:{date}"


def _enabled() -> bool:
    return os.environ.get("IGNITION_SHADOW_ENABLED", "1") == "1"


def _now_et() -> datetime:
    import zoneinfo
    return datetime.now(timezone.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))


def _secs(dt: datetime) -> int:
    return dt.hour * 3600 + dt.minute * 60 + dt.second


# ── candidate parsing (tolerant — Track A owns the writer) ─────────────────
def parse_candidates(raw: Any) -> list[dict]:
    """Parse the Track A pre-lock payload into [{ticker, gap_pct?,
    catalyst_reason?}, ...], capped at MAX_CANDIDATES. Tolerant of a JSON
    list of dicts, a {'candidates': [...]} wrapper, or a bare list of ticker
    strings; anything unparseable yields []."""
    if not raw:
        return []
    data = raw
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except Exception:
            return []
    if isinstance(data, dict):
        data = data.get("candidates") or data.get("picks") or []
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if isinstance(item, str):
            item = {"ticker": item}
        if not isinstance(item, dict):
            continue
        tk = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
        if not tk:
            continue
        out.append({**item, "ticker": tk})
        if len(out) >= MAX_CANDIDATES:
            break
    return out


# ── row construction ───────────────────────────────────────────────────────
def build_shadow_row(candidate: dict, *, entry: float, stop: float) -> dict:
    """Shape one email_signals_history shadow row for an OR break. Long only:
    entry = break price, stop = quote-sampled OR-low, target = entry + 2R.
    score = gap_pct per the shadow-cohort convention (the resolver ranks on
    realized R, not score)."""
    entry = float(entry)
    stop = float(stop)
    if stop >= entry:  # defensive — cannot happen when OR-low <= OR-high < entry
        stop = round(entry * 0.999, 4)
    risk = entry - stop
    target = entry + 2.0 * risk
    gap = 0.0
    try:
        gap = float(candidate.get("gap_pct") or candidate.get("gap") or 0.0)
    except Exception:
        pass
    catalyst = (candidate.get("catalyst_reason") or candidate.get("catalyst")
                or candidate.get("reason") or None)
    why = [f"ignition OR-break >= {BREAK_PCT}% above quote-sampled OR-high",
           "opening range 09:30:20-09:31:30 ET (15s quote-short samples)"]
    return {
        "ticker": candidate["ticker"],
        "asset_type": "stocks",
        "direction": "long",
        "entry": round(entry, 4),
        "stop": round(stop, 4),
        "target": round(target, 4),
        "gap_pct": gap,
        "rel_vol": 0,
        "today_vol": 0,
        "score": gap,
        "catalyst_reason": catalyst,
        "quality_reasons": why,
        "stop_reason": "quote-sampled opening-range low (09:30:20-09:31:30 ET)",
        "target_reason": "2R above OR-break entry",
        "matched_strategy": MATCHED_STRATEGY,
        "shadow": True,
        "rr": 2.0,
        "projected_move_pct": round((target - entry) / entry * 100.0, 2) if entry else 0.0,
        "why_selected": why,
        "instrument_type": "watch_only",
    }


async def persist_shadow_row(row: dict) -> bool:
    """INSERT one ignition shadow row. Column list mirrors the daily shadow
    scan INSERT (app/engines/scanner/shadow.py) so the resolver treats both
    cohorts identically. Dedup: one (ticker, ignition_shadow, ET-day) row.
    Returns True only when a row was written; never raises."""
    try:
        from sqlalchemy import text as _t
        from app.database import async_session_factory
        async with async_session_factory() as db:
            dup = (await db.execute(_t(
                "SELECT 1 FROM email_signals_history WHERE ticker=:tk AND matched_strategy=:ms "
                "AND picked_at::date = (NOW() AT TIME ZONE 'America/New_York')::date LIMIT 1"
            ), {"tk": row["ticker"], "ms": MATCHED_STRATEGY})).first()
            if dup:
                logger.info(f"[ignition-shadow] {row['ticker']} already recorded today — skip")
                return False
            await db.execute(_t("""
                INSERT INTO email_signals_history
                  (picked_at, ticker, asset_type, direction, entry, stop, target,
                   gap_pct, rel_vol, today_vol, score, catalyst_reason, quality_reasons,
                   stop_reason, target_reason, matched_strategy, shadow, rr,
                   projected_move_pct, why_selected, instrument_type)
                VALUES (NOW(), :tk, 'stocks', 'long', :en, :st, :tg, :gp, :rv, :tv, :sc,
                        :cr, :qr, :sr, :tr, :ms, true, :rr, :pm, :wy, 'watch_only')
            """), {
                "tk": row["ticker"], "en": row["entry"], "st": row["stop"],
                "tg": row["target"], "gp": row["gap_pct"], "rv": row["rel_vol"],
                "tv": int(row["today_vol"] or 0), "sc": row["score"],
                "cr": row["catalyst_reason"],
                "qr": json.dumps(row["quality_reasons"]),
                "sr": row["stop_reason"], "tr": row["target_reason"],
                "ms": row["matched_strategy"], "rr": row["rr"],
                "pm": row["projected_move_pct"],
                "wy": json.dumps(row["why_selected"]),
            })
            await db.commit()
        logger.info(f"[ignition-shadow] persisted {row['ticker']} entry={row['entry']} "
                    f"stop={row['stop']} target={row['target']}")
        return True
    except Exception as e:
        logger.error(f"[ignition-shadow] persist failed for {row.get('ticker')}: {e}")
        return False


# ── core window loop (fully injected — deterministic in tests) ─────────────
async def run_ignition_window(
    candidates: list[dict],
    *,
    fetch_price: Callable[[str], Awaitable[Optional[float]]],
    persist: Callable[[dict], Awaitable[bool]],
    now_fn: Callable[[], datetime] = _now_et,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    poll_seconds: float = POLL_SECONDS,
    max_rows: int = IGNITION_MAX_ROWS_PER_DAY,
) -> dict:
    """Poll quote-short for each candidate every ~poll_seconds until 09:36 ET.
    09:30:20-09:31:30 samples build the quote-sampled opening range; after
    that, a price >= OR-high * (1 + BREAK_PCT%) fires one shadow row per
    ticker (max `max_rows` total). Candidates with no successful OR sample
    (all quote fetches failed) never arm and are skipped. Per-quote failures
    fail open (skip that sample). Returns a summary dict for logging/tests."""
    cands = list(candidates)[:MAX_CANDIDATES]
    or_low: dict[str, float] = {}
    or_high: dict[str, float] = {}
    fired: list[dict] = []
    fired_tickers: set[str] = set()
    while True:
        now = now_fn()
        s = _secs(now)
        if s >= WINDOW_END_S or len(fired) >= max_rows:
            break
        if s >= OR_START_S:
            in_or = s <= OR_END_S
            for cand in cands:
                tk = cand["ticker"]
                if tk in fired_tickers:
                    continue
                try:
                    px = await fetch_price(tk)
                except Exception as e:  # fail-open per sample
                    logger.warning(f"[ignition-shadow] quote {tk} failed: {e}")
                    px = None
                if not px or px <= 0:
                    continue
                px = float(px)
                if in_or:
                    or_low[tk] = min(or_low.get(tk, px), px)
                    or_high[tk] = max(or_high.get(tk, px), px)
                    continue
                hi, lo = or_high.get(tk), or_low.get(tk)
                if hi is None or lo is None:
                    continue  # no OR sample -> never arms (documented)
                if px >= hi * (1.0 + BREAK_PCT / 100.0):
                    row = build_shadow_row(cand, entry=px, stop=lo)
                    try:
                        ok = await persist(row)
                    except Exception as e:  # fail-open
                        logger.error(f"[ignition-shadow] persist raised for {tk}: {e}")
                        ok = False
                    fired_tickers.add(tk)  # one attempt per ticker per day
                    if ok:
                        fired.append(row)
                    if len(fired) >= max_rows:
                        break
        await sleep(poll_seconds)
    summary = {"candidates": len(cands), "fired": len(fired),
               "fired_tickers": sorted(t["ticker"] for t in fired),
               "or_low": or_low, "or_high": or_high}
    logger.info(f"[ignition-shadow] window done {summary['fired']}/{summary['candidates']} "
                f"fired={summary['fired_tickers']}")
    return summary


# ── daily entrypoint (latch + candidate load + window) ─────────────────────
async def run_ignition_shadow_once(*, redis_client=None) -> dict:
    """One daily shadow run: acquire the redis latch, load the Track A
    pre-lock candidates, wait for 09:30:20 ET, run the window. SHADOW ONLY —
    no emails, no orders, no routing. Never raises."""
    try:
        if not _enabled():
            return {"status": "disabled"}
        et = _now_et()
        if et.weekday() >= 5:
            return {"status": "weekend"}
        date_key = et.strftime("%Y-%m-%d")
        r = redis_client
        if r is None:
            import redis.asyncio as _redis
            r = _redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                                decode_responses=True)
        try:
            got = await r.set(LATCH_KEY_FMT.format(date=date_key), "running",
                              ex=36 * 3600, nx=True)
        except Exception as e:
            logger.warning(f"[ignition-shadow] redis latch failed ({e}) — skip (no dup risk)")
            return {"status": "no-redis"}
        if not got:
            return {"status": "latched"}
        try:
            raw = await r.get(CANDIDATES_KEY_FMT.format(date=date_key))
        except Exception as e:
            logger.warning(f"[ignition-shadow] candidates read failed: {e}")
            raw = None
        cands = parse_candidates(raw)
        if not cands:
            logger.info(f"[ignition-shadow] no Track A candidates for {date_key} — exit")
            return {"status": "no-candidates"}
        logger.info(f"[ignition-shadow] {date_key} start — "
                    f"{[c['ticker'] for c in cands]} (shadow only)")
        # wait for the 09:30:20 OR start if spawned early
        s = _secs(_now_et())
        if s < OR_START_S:
            await asyncio.sleep(max(0.0, OR_START_S - s))
        from app.engines.data_feeds.fmp_feed import fetch_quote_short_price
        summary = await run_ignition_window(
            cands, fetch_price=fetch_quote_short_price, persist=persist_shadow_row)
        return {"status": "done", **summary}
    except Exception as e:  # fail-open — a shadow cohort must never break prod
        logger.error(f"[ignition-shadow] run failed: {e}")
        return {"status": "error", "error": str(e)}


# ── scheduler hook — sync, spawns its own task, never blocks the loop ──────
_spawned_dates: set[str] = set()


def maybe_spawn_ignition_shadow(*, _now_et_fn=None) -> bool:
    """Called from the scheduler loop each tick. Between 09:29:00 and
    09:36:00 ET on weekdays (env gate IGNITION_SHADOW_ENABLED, default on)
    it spawns run_ignition_shadow_once on its OWN asyncio task exactly once
    per process per day; the redis latch dedupes across restarts/workers.
    Returns True only when a task was spawned. Never raises."""
    try:
        if not _enabled():
            return False
        et = (_now_et_fn or _now_et)()
        if et.weekday() >= 5:
            return False
        s = _secs(et)
        if not (SPAWN_START_S <= s < WINDOW_END_S):
            return False
        date_key = et.strftime("%Y-%m-%d")
        if date_key in _spawned_dates:
            return False
        _spawned_dates.add(date_key)

        async def _task():
            try:
                res = await run_ignition_shadow_once()
                logger.info(f"[ignition-shadow] daily task finished: {res.get('status')}")
            except Exception as e:  # belt & braces — run_once already swallows
                logger.error(f"[ignition-shadow] task crashed: {e}")

        asyncio.create_task(_task())
        logger.info(f"[ignition-shadow] spawned daily task for {date_key}")
        return True
    except Exception as e:
        logger.warning(f"[ignition-shadow] spawn check failed: {e}")
        return False
