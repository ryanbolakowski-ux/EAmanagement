"""Saro 1.3 LIVE pick engine — predictive consolidation on the money path.

SARO_PICK_ENGINE='compression' (the DEFAULT) routes theta_scanner.
find_best_premarket_pick here; SARO_PICK_ENGINE='momentum' + `up -d`
reverts to the legacy funnel/gapper path with ZERO code changes (the legacy
body is byte-for-byte untouched below the dispatch). Owner (Ryan)
explicitly chose "Replace with compression logic now" (2026-08-18,
informed three-option prompt).

WHAT THIS DOES (and deliberately nothing else — candidate selection ONLY;
everything downstream of the returned dict, emit_theta_pick included, is
the untouched legacy path):

  * PREMARKET/EARLY (first scheduler tick of the day): build the day's
    compression candidate list via the ALREADY-DEPLOYED saro13 screen
    (screen.run_screen — S1+S2 daily-bar compression, anti-junk universe,
    M&A HARD EXCLUDE; <= 42 FMP calls/day worst case, every call
    redis-cached per ET day), rank it by compression score (tightest coil
    first — run_screen's own order is dollar-volume, it carries no score),
    cache it under an ET-day key, and refresh it ONCE at/after 9:30 iff it
    came up empty (retry-after-outage; cached inputs make that ~0 calls).

  * INTRADAY (the scanner job's EXISTING cadence — 1-min ticks 9:33-10:00,
    5-min to 12:00; this module spawns NO loops/tasks of its own): for the
    TOP-10 candidates, check the Section-2 ALERT on fresh 1-min bars:
    TTM squeeze ON at the PRIOR bar + close breaking ABOVE the upper
    Keltner + day RVOL >= 4.0x pro-rata (saro12.indicators.alert_state —
    imported, not forked; KC 20 / 1.5x ATR(20), BB 20/2, >= 40 bars).
    NOTE (cadence honesty): 10:00-12:00 runs on 5-min ticks, so a breakout
    bar can be seen up to ~5 minutes late mid-morning — accepted by spec.

  * FIRST candidate to alert = THE pick, returned in the EXACT legacy
    emit_theta_pick shape (field-for-field vs a recorded legacy call —
    tests/test_live_pick_compression.py). Entry is the ALERT-BAR close
    (live-price entry honesty, FDUS 2026-07-07 rule); stop/target come
    from saro13 shadow.build_levels structure RE-BASED onto that live
    entry (stop = coil floor / swing low when below entry, else -3%;
    target = entry + 2x compression range — the +% target convention).
    A screener-vs-alert-close divergence > 3% is a HARD skip
    (theta_scanner._stale_quote_check — the compression path bypasses
    _apply_quality_filters, so the stale-quote gate is applied HERE).

  * LATCH-BURN PROTECTION: run_theta_scanner_for_all_users re-calls
    find_best_premarket_pick AFTER the theta_fired latch is burned. The
    alerted pick is therefore cached (redis, ET-day key) BEFORE it is
    returned and served VERBATIM on every later call that day; an
    in-process fallback cache covers a redis blip between the two calls.

EXPECTATION SETTING (owner-informed, NOT a defect): compression alerts are
RARE — day one of the saro13 shadow found 0 coils. More no-pick days is
the INTENDED behavior; every no-alert day writes an honest, per-day reason
into theta_scanner._NOPICK_STATE for the existing no-pick email machinery.

SCORE CONVENTION: emit + the scanner job hard-require pick['score'], and
the job fires only score >= 10.0 (9:33-12:00, _min_score_for_et). A
compression ALERT is already the confirmed setup, so
    score = 10.0 + day-RVOL   (>= 14.0 by construction since RVOL >= 4.0)
— deterministic, and cached with the pick so both scheduler calls agree.

BUDGET (why net FMP usage DROPS vs momentum): screen <= 42 calls/day
(1 screener + <=30 EOD + 1 M&A + <=10 news, all day-cached; the premarket
build and the 9:30 refresh share the same cache) + alert checks <= 10
fetch_intraday_bars calls/tick (cheap 1-min-chart endpoint, 15s-TTL
cached, never raises, stops at the first alert / fired latch). The
momentum morning batch (~79 calls: market snapshot + 12-finalist
enrichment + per-candidate quality bar pulls, re-run every tick) is
SKIPPED entirely when this engine is active. RVOL denominator costs ZERO
extra calls: mean daily volume over the last ~63 completed sessions from
the saro13 EOD day-cache that run_screen already populated per candidate.

WHAT THIS MODULE NEVER TOUCHES: the theta_fired latch, blackout windows,
entry-expiry / MOO cutoff, the pending-entry watcher, pick_router/paper,
emails, or the saro13 SHADOW cohort's latch (theta:saro13:{date}) and
persist path — the shadow keeps running independently (its warm saro13:*
day caches are shared read-only, a benefit).
"""
from __future__ import annotations

import json
from typing import Optional

from loguru import logger

from app.engines.scanner.saro13 import config as C

# ── live-path tunables (screen/alert math constants live in saro13.config
#    and saro12.config — imported, never forked; pinned by the tests) ──────
INTRADAY_TICKER_CAP = 10            # alert checks per scan tick (budget cap)
ALERT_START_MIN = 9 * 60 + 33       # ET minutes: alert checks 09:33 ..
ALERT_END_MIN = 12 * 60             # .. 12:00 (mirrors _min_score_for_et)
REFRESH_MIN = 9 * 60 + 30           # one screen retry at/after 09:30 if empty
RVOL_AVG_SESSIONS = 63              # ~3 months of completed sessions
RVOL_MIN_SESSIONS = 20              # fewer -> RVOL unknown -> leg fails closed
STALE_MAX_DIFF_PCT = 3.0            # screener-vs-alert-close hard-skip gate

PICK_KEY_FMT = "saro13:live:pick:{date}"           # the day's alerted pick
CANDS_KEY_FMT = "saro13:live:cands:{date}"         # ranked candidate list
REFRESH_KEY_FMT = "saro13:live:refresh930:{date}"  # one-shot 9:30 retry marker
PICK_TTL_S = 36 * 3600

# In-process fallback caches (same process as both scheduler call sites):
# a redis blip between the latch burn and the re-call must never turn an
# alerted day into a latched zero-email day.
_MEM: dict = {"pick": {}, "cands": {}, "refresh930": set()}


# ── small pure helpers ─────────────────────────────────────────────────────
def _compression_score(cand: dict) -> float:
    """Coil tightness = bb_width_thresh / bb_width (>= 1.0 means at/inside
    the lowest-15% width threshold; HIGHER = tighter). run_screen output is
    dollar-volume-ordered and score-free — 'top-10 by compression score'
    is pinned here. Falls back to 1/bb_width, then 0 (rank last)."""
    try:
        d = cand.get("comp_detail") or {}
        w = float(cand.get("bb_width") or d.get("bb_width") or 0)
        th = float(d.get("bb_width_thresh") or 0)
        if w > 0 and th > 0:
            return round(th / w, 4)
        if w > 0:
            return round(1.0 / w, 4)
    except Exception:
        pass
    return 0.0


def _watch_reason(cands: list) -> str:
    """Honest no-pick reason: rare alerts are the design, say so per day."""
    n = min(len(cands), INTRADAY_TICKER_CAP)
    watched = ", ".join(str(c.get("ticker")) for c in cands[:INTRADAY_TICKER_CAP])
    return (f"{n} coiled candidate(s) watched 9:33-12:00 ET ({watched}); none broke "
            f"above the upper Keltner with day RVOL >= 4x pro-rata — compression "
            f"alerts are rare by design (a no-pick day is the expected outcome)")


def _set_nopick(reason: Optional[str], ticker=None, score=None) -> None:
    """Write the reason where the after-window no-pick guard reads it
    (theta_scanner._NOPICK_STATE — getattr'd by the scheduler, fail-open).
    reason=None mirrors the legacy 'a pick fired today' clear."""
    try:
        from app.engines.options import theta_scanner as _ts
        _ts._NOPICK_STATE["last"] = (None if reason is None else
                                     {"ticker": ticker, "score": score, "reason": reason})
    except Exception:
        pass


# ── candidate list (built premarket, ET-day cached, one 9:30 empty retry) ──
async def _build_candidates(r, date_key: str) -> list:
    from app.engines.scanner.saro13.screen import run_screen
    cands = await run_screen(r=r, date_key=date_key) or []
    cands = sorted(cands, key=_compression_score, reverse=True)
    _MEM["cands"] = {date_key: cands}  # today only (bounded)
    if r is not None:
        from app.engines.scanner.saro13.screen import _cache_set_json
        await _cache_set_json(r, CANDS_KEY_FMT.format(date=date_key), cands,
                              ttl_s=PICK_TTL_S)
    if cands:
        logger.info(f"[saro13-live] candidate list for {date_key}: {len(cands)} coil(s) — "
                    f"top: {', '.join(str(c.get('ticker')) for c in cands[:INTRADAY_TICKER_CAP])}")
    else:
        logger.info(f"[saro13-live] candidate list for {date_key}: 0 coils "
                    f"(rare-alert engine — a no-pick day is the expected outcome)")
    return cands


async def _claim_refresh(r, date_key: str) -> bool:
    """True exactly once per day (redis NX marker; in-process set when redis
    is down) — gates the 9:30 empty-list screen retry."""
    if date_key in _MEM["refresh930"]:
        return False
    _MEM["refresh930"] = {date_key}
    if r is not None:
        try:
            got = await r.set(REFRESH_KEY_FMT.format(date=date_key), "1",
                              ex=PICK_TTL_S, nx=True)
            return bool(got)
        except Exception:
            return True  # redis down — the in-process marker still caps us at once
    return True


async def _day_candidates(r, date_key: str, et_min: int) -> list:
    cands = _MEM["cands"].get(date_key)
    if cands is None and r is not None:
        from app.engines.scanner.saro13.screen import _cache_get_json
        cands = await _cache_get_json(r, CANDS_KEY_FMT.format(date=date_key))
        if cands is not None:
            _MEM["cands"] = {date_key: cands}
    if cands is None:
        cands = await _build_candidates(r, date_key)      # premarket/first build
    elif not cands and et_min >= REFRESH_MIN and await _claim_refresh(r, date_key):
        logger.info("[saro13-live] 9:30 retry — premarket screen was empty, re-running once")
        cands = await _build_candidates(r, date_key)
    return cands or []


# ── RVOL denominator (zero extra FMP calls) ────────────────────────────────
async def _avg_daily_volume(r, date_key: str, ticker: str) -> Optional[float]:
    """Mean daily volume of the last RVOL_AVG_SESSIONS COMPLETED sessions
    from the saro13 EOD day-cache (populated by run_screen for every
    candidate this morning). saro12 sources this from Yahoo; the live path
    must not add a data dependency. None (-> RVOL leg fails closed) when
    the cache is missing or holds < RVOL_MIN_SESSIONS usable sessions."""
    rows = None
    if r is not None:
        try:
            from app.engines.scanner.saro13.screen import _cache_get_json
            rows = await _cache_get_json(r, C.DAILY_CACHE_FMT.format(date=date_key,
                                                                     ticker=ticker))
        except Exception:
            rows = None
    if isinstance(rows, dict):
        rows = rows.get("historical")  # tolerate the wrapped variant
    if not rows:
        return None
    vols: list = []
    for r0 in rows:
        if not isinstance(r0, dict):
            continue
        try:
            d = str(r0.get("date") or "")[:10]
            if not d or d >= str(date_key):
                continue  # today's row = live partial, never a baseline
            v = float(r0.get("volume", 0) or 0)
            if v > 0:
                vols.append((d, v))
        except Exception:
            continue
    if len(vols) < RVOL_MIN_SESSIONS:
        return None
    vols.sort(key=lambda x: x[0], reverse=True)
    recent = [v for _, v in vols[:RVOL_AVG_SESSIONS]]
    return sum(recent) / len(recent)


# ── levels: saro13 structure re-based onto the live alert-bar entry ────────
def _levels_from_alert(cand: dict, live_entry: float) -> dict:
    """shadow.build_levels derives the structural stop (coil floor / swing
    low) off the stale daily close; live picks must be executable, so the
    ENTRY is the alert-bar close and stop/target are re-based onto it:
    the structural stop is an ABSOLUTE daily level (kept when still below
    the live entry), the target re-projects entry + 2x compression range
    from the live entry (same measured-move convention). Always returns
    stop < entry < target."""
    from app.engines.scanner.saro13.shadow import build_levels
    lv = build_levels(cand)  # structural stop + canonical reasons
    entry = round(float(live_entry), 4)
    stop, stop_reason = float(lv["stop"]), str(lv["stop_reason"])
    placeholder = "fallback" in stop_reason
    if not (0 < stop < entry):
        stop = round(entry * (1.0 - C.FALLBACK_STOP_PCT / 100.0), 4)
        stop_reason = (f"fallback -{C.FALLBACK_STOP_PCT:g}% under the live alert-bar "
                       f"entry (structural stop not below entry)")
        placeholder = True
    risk = entry - stop
    rng = 0.0
    try:
        rng = float(cand.get("compression_range") or 0.0)
    except Exception:
        pass
    if rng > 0:
        target = round(entry + C.TARGET_RANGE_MULT * rng, 4)
        target_reason = (f"entry + {C.TARGET_RANGE_MULT:g}x compression range "
                         f"({rng:.4g} = upperBB - lowerBB coil width, measured move; "
                         f"re-based to the live alert-bar entry)")
    else:
        target = round(entry + 2.0 * risk, 4)
        target_reason = "fallback 2R (degenerate compression range)"
    if target <= entry:  # belt & braces
        target = round(entry + 2.0 * risk, 4)
        target_reason = "fallback 2R (guard)"
    rr = round((target - entry) / risk, 2) if risk > 0 else C.TARGET_RANGE_MULT
    return {"entry": entry, "stop": round(stop, 4), "target": target, "rr": rr,
            "stop_reason": stop_reason, "target_reason": target_reason,
            "stop_is_placeholder": placeholder}


# ── pick construction (the EXACT legacy emit_theta_pick field set) ─────────
def _build_pick(cand: dict, st: dict, rv: float, today_cum: float,
                ranked: list) -> dict:
    from app.engines.scanner.saro12 import config as C12
    entry = round(float(st["close"]), 4)
    lv = _levels_from_alert(cand, entry)
    prev_close = 0.0
    try:
        prev_close = float(cand.get("close") or 0)  # last COMPLETED daily close
    except Exception:
        pass
    if prev_close > 0:
        gap_pct = round((entry / prev_close - 1.0) * 100.0, 2)
    else:
        gap_pct = round(float(cand.get("live_chg_pct") or 0.0), 2)
    kc_upper = st.get("kc_upper")
    quality = [
        (f"saro13 coil: BB width {cand.get('bb_width')} (lowest "
         f"{C.BBWIDTH_LOWEST_PCT:g}% of {C.BBWIDTH_RANGE_SESSIONS}d), "
         f"RSI {cand.get('rsi')}, vol ratio {cand.get('vol_ratio')}"),
        "TTM squeeze ON at the prior 1-min bar",
        (f"close ${entry:.2f} broke above the upper Keltner ${float(kc_upper):.2f}"
         if kc_upper else "close broke above the upper Keltner"),
        f"day RVOL {rv:.1f}x >= {C12.RVOL_MIN:g}x (pro-rata)",
        str((cand.get("screen_reasons") or ["M&A screen: n/a"])[-1]),  # the M&A note
    ]
    alternatives = []
    for c in ranked[:INTRADAY_TICKER_CAP + 1]:
        if c is cand or c.get("ticker") == cand.get("ticker"):
            continue
        alternatives.append({"ticker": c.get("ticker"),
                             "score": _compression_score(c),
                             "gap_pct": round(float(c.get("live_chg_pct") or 0.0), 1)})
        if len(alternatives) >= 5:
            break
    return {
        # REQUIRED by emit_theta_pick / the scanner job (KeyError if missing)
        "ticker": cand["ticker"],
        "price": entry,                      # sizing basis = live alert close
        "entry": entry,                      # live-price entry honesty (FDUS rule)
        "stop": lv["stop"],
        "target": lv["target"],
        "gap_pct": gap_pct,                  # live day move vs last completed close
        "rel_vol": round(float(rv), 2),      # email renders "{rel_vol}x"
        "today_vol": int(today_cum),         # history INSERT :tv
        "score": round(10.0 + float(rv), 1),  # SCORE CONVENTION (module docstring)
        "catalyst_reason": (f"compression breakout — TTM squeeze fired above the "
                            f"upper Keltner (day RVOL {rv:.1f}x)"),
        "projected_move_pct": round((lv["target"] / entry - 1.0) * 100.0, 2),
        # consumed via .get (optional-but-expected legacy fields)
        "live_price": entry,                 # _entry_basis contract
        "watch_only": False,                 # an alert IS the confirmed setup
        "quality_reasons": quality,
        "alternatives": alternatives,
        "stop_reason": lv["stop_reason"],
        "target_reason": lv["target_reason"],
        "rr": lv["rr"],
        "levels_basis": "saro13_alert_bar",
        "stop_is_placeholder": lv["stop_is_placeholder"],
        "asset_type": "options",             # legacy UI/health key (it is a stock)
        "matched_strategy": "saro13:compression_live",  # NOT the shadow's _coil tag
    }


# ── the per-tick alert scan (existing cadence; <= 10 bar fetches/tick) ─────
async def _alert_scan(r, cands: list, et_min: int, date_key: str) -> Optional[dict]:
    from app.engines.data_feeds.fmp_feed import fetch_intraday_bars
    from app.engines.options.theta_scanner import _stale_quote_check
    from app.engines.scanner.saro12.indicators import alert_state, bars_to_df
    from app.engines.scanner.saro12.indicators import rvol as rvol_base

    watch = cands[:INTRADAY_TICKER_CAP]
    # saro12 pro-rata recipe verbatim: session fraction floored at 5%
    sess_frac = max(0.05, min(1.0, (et_min - 570) / 390.0))
    checked = []
    for cand in watch:
        tkr = str(cand.get("ticker") or "")
        if not tkr:
            continue
        try:
            bars = await fetch_intraday_bars(tkr, date_et=date_key)
            checked.append(tkr)
            if not bars:
                continue
            df = bars_to_df(bars)
            today_cum = 0.0
            for b in bars:
                try:
                    today_cum += float(b.get("v", 0) or 0)
                except Exception:
                    continue
            avg_vol = await _avg_daily_volume(r, date_key, tkr)
            rv = rvol_base(today_cum, avg_vol)
            if rv is None:
                logger.debug(f"[saro13-live] {tkr}: no 3m avg volume in the saro13 "
                             f"EOD cache — RVOL unknown, alert leg fails closed")
                continue
            rv = rv / sess_frac
            st = alert_state(df, rv)
            if not st.get("alert"):
                logger.debug(f"[saro13-live] {tkr}: no alert (bars={st.get('bars')} "
                             f"squeeze_prev={st.get('squeeze_prev')} "
                             f"breakout={st.get('breakout')} "
                             f"rvol_ok={st.get('rvol_ok')} rvol={rv:.2f})")
                continue
            # STALE-QUOTE / ALREADY-RAN gate (the compression path bypasses
            # _apply_quality_filters, so its 3% hard reject is applied here):
            # alert close > 3% away from the morning screener price means a
            # stale snapshot or a coil that already broke — we only chase
            # the FIRST break.
            is_stale, diff = _stale_quote_check(cand.get("price"), st.get("close"),
                                                STALE_MAX_DIFF_PCT)
            if is_stale:
                logger.warning(f"[saro13-live] {tkr}: ALERT but close "
                               f"${float(st.get('close') or 0):.2f} is {diff:+.1f}% vs the "
                               f"morning screener price ${float(cand.get('price') or 0):.2f} "
                               f"(> {STALE_MAX_DIFF_PCT:g}%) — skip (first-break doctrine)")
                continue
            pick = _build_pick(cand, st, rv, today_cum, cands)
            logger.info(f"[saro13-live] ALERT {tkr}: close ${pick['entry']:.2f} > "
                        f"KC-upper ${float(st.get('kc_upper') or 0):.2f}, squeeze_prev=True, "
                        f"RVOL {rv:.1f}x — PICK (score={pick['score']}, "
                        f"stop={pick['stop']}, target={pick['target']}, rr={pick['rr']})")
            return pick
        except Exception as e:
            logger.error(f"[saro13-live] {tkr}: alert check failed "
                         f"({type(e).__name__}: {e}) — next candidate")
            continue
    if checked:
        logger.info(f"[saro13-live] tick scan: 0/{len(checked)} alerts "
                    f"({', '.join(checked)})")
    return None


# ── redis persistence of the day's pick (latch-burn protection) ────────────
async def _persist_pick(r, date_key: str, pick: dict) -> None:
    _MEM["pick"] = {date_key: pick}
    if r is None:
        return
    try:
        from app.engines.scanner.saro13.screen import _cache_set_json
        await _cache_set_json(r, PICK_KEY_FMT.format(date=date_key), pick,
                              ttl_s=PICK_TTL_S)
    except Exception as e:
        logger.warning(f"[saro13-live] pick cache write failed: {e}")
    # frontend "Today's Pick" widget — same keys/convention as both legacy paths
    try:
        from datetime import date as _d, datetime as _dt, timezone as _tz
        payload = dict(pick)
        payload["picked_at"] = _dt.now(_tz.utc).isoformat()
        raw = json.dumps(payload, default=str)
        await r.setex(f"theta:lastpick:{_d.today().isoformat()}", PICK_TTL_S, raw)
        await r.setex("theta:lastpick:latest", PICK_TTL_S, raw)
    except Exception as e:
        logger.warning(f"[saro13-live] lastpick redis write failed: {e}")


# ── entrypoint (the SARO_PICK_ENGINE='compression' target) ─────────────────
async def find_compression_pick(db, *, redis_client=None) -> Optional[dict]:
    """Drop-in replacement for the momentum find_best_premarket_pick(db):
    returns Optional[dict] in the exact legacy emit shape, or None (the
    existing no-pick machinery handles the rest). `db` is accepted for
    call-site compatibility; the compression path does not use it.
    `redis_client` is injectable for tests; production builds its own."""
    from app.engines.options.ignition_shadow import _now_et
    et = _now_et()
    date_key = et.strftime("%Y-%m-%d")
    et_min = et.hour * 60 + et.minute

    r = redis_client
    own_r = r is None
    if r is None:
        try:
            from app.engines.scanner.saro13.screen import _redis
            r = _redis()
        except Exception as e:
            logger.warning(f"[saro13-live] no redis client ({e}) — in-process caches only")
    try:
        # (0) LATCH-BURN PROTECTION FIRST: once alerted, the cached pick is
        # returned VERBATIM for the rest of the day (the post-latch re-call
        # inside run_theta_scanner_for_all_users must be deterministic).
        cached = _MEM["pick"].get(date_key)
        if cached is None and r is not None:
            try:
                from app.engines.scanner.saro13.screen import _cache_get_json
                cached = await _cache_get_json(r, PICK_KEY_FMT.format(date=date_key))
            except Exception:
                cached = None
        if cached:
            _MEM["pick"] = {date_key: cached}
            _set_nopick(None)  # a pick fired today (legacy convention)
            logger.info(f"[saro13-live] returning cached {date_key} pick "
                        f"{cached.get('ticker')} (deterministic re-call)")
            return cached

        if et.weekday() >= 5:
            _set_nopick("weekend — market closed (compression engine idle)")
            return None

        # (1) the day's candidate list (premarket build, one 9:30 empty retry)
        cands = await _day_candidates(r, date_key, et_min)
        if not cands:
            _set_nopick("saro13 compression screen found 0 coiled candidates today — "
                        "compression alerts are rare by design; a no-pick day is "
                        "the expected outcome, not a defect")
            return None

        # (2) alert checks only inside 9:33-12:00 ET (premarket the job's
        # threshold is 99 anyway — we also skip the bar fetches entirely)
        if et_min < ALERT_START_MIN:
            logger.info(f"[saro13-live] {len(cands)} coiled candidate(s) staged "
                        f"({', '.join(str(c.get('ticker')) for c in cands[:INTRADAY_TICKER_CAP])}) "
                        f"— alert checks begin 09:33 ET")
            _set_nopick(_watch_reason(cands))
            return None
        if et_min > ALERT_END_MIN:
            _set_nopick(_watch_reason(cands))
            return None

        # (3) Section-2 alert scan on the top-10 (first alert wins)
        pick = await _alert_scan(r, cands, et_min, date_key)
        if pick is None:
            _set_nopick(_watch_reason(cands))
            return None

        # (4) cache BEFORE returning — the post-latch re-call must see it
        await _persist_pick(r, date_key, pick)
        _set_nopick(None)
        return pick
    finally:
        if own_r and r is not None:
            try:
                await r.aclose()
            except Exception:
                pass
