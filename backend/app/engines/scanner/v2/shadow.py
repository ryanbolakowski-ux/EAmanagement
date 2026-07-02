"""Scanner V2 daily SHADOW scan (SCANNER-V2). Persist-only, fully isolated.

Mirrors the V1 shadow pattern (app.engines.scanner.shadow): reuse the SAME
funnel stage-0/1 candidate generation across the approved equity templates,
then RE-RANK with score_v2 and apply the V2 fire gates for a hypothetical
would-it-have-fired flag. The top candidates are persisted to
email_signals_history with shadow=true and matched_strategy prefixed `v2:`
(e.g. v2:momentum_breakout) so the existing outcome resolver scores them
against real candles — a clean forward-test cohort for the V2 ranking,
side-by-side with V1's, with ZERO effect on emails, trades, or the V1 rows.

Isolation: own Redis once-per-ET-day latch (theta:shadow_v2:{date}), own env
flag (SCANNER_V2_SHADOW_ENABLED, default on), every stage wrapped so a failure
here can never touch the real scan or the V1 shadow.
"""
from __future__ import annotations

import os
import json
from loguru import logger

V2_PREFIX = "v2:"

# Hypothetical fire probes: a shadow scan runs once after the open, so we ask
# the gates "would this have fired?" at a representative premarket time and a
# representative RTH-window time instead of the (post-close) scan time.
_HYPOTHETICAL_FIRE_TIMES = ((8 * 60, "premarket 08:00 ET"), (9 * 60 + 40, "RTH 09:40 ET"))


def _hypothetical_fire(candidate: dict) -> tuple:
    """(would_fire, fired_at_label, FireDecision) — first window that allows,
    else the last (RTH) refusal so the persisted note explains the block."""
    from app.engines.scanner.v2.gates import decide_fire
    last = None
    for t, label in _HYPOTHETICAL_FIRE_TIMES:
        d = decide_fire(t, candidate)
        last = d
        if d.allowed:
            return True, label, d
    return False, None, last


async def run_v2_shadow_scan(db, *, top_n_per_template: int = 3, max_unique: int = 40,
                             persist_top: int = 5) -> dict:
    """Re-rank the day's funnel candidates under score_v2 + V2 fire gates and
    persist the top-`persist_top` as v2-prefixed shadow rows. Returns a summary.
    Persist-only — never emits, never trades, never touches V1 rows."""
    from app.engines.options.momentum_scanner import _fetch_market_snapshot
    from app.engines.options.theta_scanner import (
        _get_8k_catalyst, _premarket_dollar_volume, _session_vwap, _last3_higher_highs,
    )
    from app.engines.options.premarket_scheduler import _polygon_1min_bars, _today_et_date_str
    from app.engines.scanner.definitions import equity_templates
    from app.engines.scanner.funnel import _coarse
    from app.engines.scanner.levels import compute_levels
    from app.engines.scanner.shadow import _ensure_columns
    from app.engines.scanner.v2.scoring import score_v2
    from sqlalchemy import text as _t

    await _ensure_columns(db)
    templates = equity_templates()
    rows = await _fetch_market_snapshot() or []

    # Market context from the SAME snapshot (no extra network). Missing/zero
    # QQQ row -> empty context -> rs/regime components sit neutral, per the
    # never-fabricate rule.
    context: dict = {}
    for r in rows:
        if (r.get("ticker") or "").upper() != "QQQ":
            continue
        try:
            c = float(r.get("day", {}).get("c") or 0)
            pc = float(r.get("prevDay", {}).get("c") or 0)
            if c > 0 and pc > 0:
                context["qqq_day_pct"] = round((c - pc) / pc * 100.0, 2)
                context["qqq_above_prev_close"] = c > pc
        except Exception:
            pass
        break

    # Stage 0/1 (reused verbatim from V1) + provisional V2 rank (no network:
    # catalyst/premkt $-vol enrich the capped unique set below).
    per_tpl = []   # (tpl, [cand, ...]) top-N per template
    need = {}      # ticker -> best provisional score (to pick the unique set)
    for tpl in templates:
        cands = [c for c in (_coarse(tpl, r) for r in rows) if c]
        for c in cands:
            c["_v2_prov"] = score_v2(c, context).total
        cands.sort(key=lambda x: x["_v2_prov"], reverse=True)
        top = cands[:top_n_per_template]
        per_tpl.append((tpl, top))
        for c in top:
            need[c["ticker"]] = max(need.get(c["ticker"], 0.0), c["_v2_prov"])
        logger.info(f"[shadow-v2] {tpl.key}: coarse={len(cands)} top={len(top)}")

    # Cap unique tickers by best provisional score — bounds Polygon calls,
    # same rate discipline as the V1 shadow.
    allowed = {tk for tk, _ in sorted(need.items(), key=lambda kv: kv[1], reverse=True)[:max_unique]}
    date_et = _today_et_date_str()
    bars_cache, cat_cache = {}, {}
    for tk in allowed:
        try:
            bars_cache[tk] = await _polygon_1min_bars(tk, date_et)
        except Exception:
            bars_cache[tk] = None
        try:
            cat_cache[tk] = await _get_8k_catalyst(db, tk)   # (weight, reason)
        except Exception:
            cat_cache[tk] = (1.0, None)

    # Full re-score with premkt $-vol + catalyst, hypothetical fire decision,
    # dedupe by ticker keeping the best (template, score) pairing.
    finalists: dict = {}   # ticker -> (tpl, cand, breakdown, would_fire, fire_at, decision)
    for tpl, top in per_tpl:
        for c in top:
            tk = c["ticker"]
            if tk not in allowed:
                continue
            enriched = dict(c)
            cat_w, cat_reason = cat_cache.get(tk, (1.0, None))
            enriched["catalyst_weight"] = cat_w
            enriched["catalyst_reason"] = cat_reason
            bars = bars_cache.get(tk)
            if bars:
                try:
                    enriched["premarket_dollar_vol"] = _premarket_dollar_volume(bars)
                except Exception:
                    pass
                # RTH confirmation proxy: above session VWAP + last-3 higher
                # highs (same primitives the V1 quality gate uses). No bars ->
                # confirmed stays False: never fire on assumed confirmation.
                try:
                    vwap = _session_vwap(bars)
                    enriched["confirmed"] = bool(
                        vwap and float(c["price"]) > float(vwap) and _last3_higher_highs(bars))
                except Exception:
                    enriched["confirmed"] = False
            bd = score_v2(enriched, context)
            would_fire, fire_at, decision = _hypothetical_fire(enriched)
            prev = finalists.get(tk)
            if prev is None or bd.total > prev[2].total:
                finalists[tk] = (tpl, enriched, bd, would_fire, fire_at, decision)

    ranked = sorted(finalists.values(), key=lambda f: f[2].total, reverse=True)[:persist_top]

    persisted, skipped, would_fire_n = 0, 0, 0
    for tpl, c, bd, would_fire, fire_at, decision in ranked:
        tk = c["ticker"]
        lv = compute_levels("long", c["price"], bars_cache.get(tk),
                            rr=tpl.levels.rr_ratio, atr_stop_mult=tpl.levels.atr_stop_mult)
        if not lv.ok:
            skipped += 1
            continue
        would_fire_n += int(would_fire)
        ms = f"{V2_PREFIX}{tpl.key}"
        # dedup per (v2-template, ticker, ET-day) — same rule as the V1 shadow;
        # the v2: prefix keeps the two forward-test cohorts fully separate.
        dup = (await db.execute(_t(
            "SELECT 1 FROM email_signals_history WHERE ticker=:tk AND matched_strategy=:ms "
            "AND picked_at::date = (NOW() AT TIME ZONE 'America/New_York')::date LIMIT 1"
        ), {"tk": tk, "ms": ms})).first()
        if dup:
            continue
        fire_note = (f"would fire {fire_at} ({decision.reason})" if would_fire
                     else f"no fire: {decision.reason}")
        await db.execute(_t("""
            INSERT INTO email_signals_history
              (picked_at, ticker, asset_type, direction, entry, stop, target,
               gap_pct, rel_vol, today_vol, score, catalyst_reason, quality_reasons,
               stop_reason, target_reason, matched_strategy, shadow, rr,
               projected_move_pct, why_selected, instrument_type)
            VALUES (NOW(), :tk, 'stocks', 'long', :en, :st, :tg, :gp, :rv, :tv, :sc,
                    :cr, :qr, :sr, :tr, :ms, true, :rr, :pm, :wy, 'watch_only')
        """), {
            "tk": tk, "en": lv.entry, "st": lv.stop, "tg": lv.target,
            "gp": c.get("gap_pct", 0), "rv": c.get("rel_vol", 0),
            "tv": int(c.get("today_vol", 0) or 0), "sc": bd.total,
            "cr": c.get("catalyst_reason"), "qr": json.dumps([fire_note]),
            "sr": lv.stop_reason, "tr": lv.target_reason, "ms": ms,
            "rr": lv.rr, "pm": lv.projected_move_pct, "wy": bd.why(),
        })
        persisted += 1
        logger.info(f"[shadow-v2] persisted {ms} {tk} score={bd.total} {fire_note}")
    await db.commit()
    summary = {"templates": len(templates), "universe": len(rows),
               "unique_tickers": len(allowed), "persisted": persisted,
               "skipped_no_levels": skipped, "would_fire": would_fire_n}
    logger.info(f"[shadow-v2] done {summary}")
    return summary


# ── scheduler hook — once per ET trading day, persist-only, fully isolated ──
async def _check_and_run_v2_shadow_scan(*, _now_et=None) -> None:
    """Called each scheduler loop; runs the V2 shadow scan exactly once per
    trading day (Redis SETNX latch theta:shadow_v2:{date}) once past ~09:45 ET
    — same cadence as the V1 shadow so the two cohorts see the same tape.
    Gated by SCANNER_V2_SHADOW_ENABLED (default on). Any failure is swallowed —
    it can never affect the real scan or the V1 shadow. `_now_et` exists only
    so tests can inject a fixed ET clock."""
    if os.environ.get("SCANNER_V2_SHADOW_ENABLED", "1") != "1":
        return
    if _now_et is None:
        from datetime import datetime as _dt, timezone as _tz
        try:
            import zoneinfo
            _now_et = _dt.now(_tz.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
        except Exception:
            return
    et = _now_et
    if et.weekday() >= 5:
        return
    et_min = et.hour * 60 + et.minute
    if et_min < 9 * 60 + 45 or et_min > 16 * 60:   # run once after the open
        return
    today_key = et.strftime("%Y-%m-%d")
    try:
        import redis as _redis
        _r = _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                                   decode_responses=True)
        if not _r.set(f"theta:shadow_v2:{today_key}", "running", ex=36 * 3600, nx=True):
            return  # already ran today (or another worker is running it)
    except Exception:
        return  # no redis latch → skip rather than risk duplicate daily runs
    try:
        from app.database import async_session_factory as _asf
        logger.info(f"[shadow-v2] daily run start {et.strftime('%H:%M ET')} ({today_key})")
        async with _asf() as db:
            await run_v2_shadow_scan(db)
    except Exception as e:
        logger.error(f"[shadow-v2] daily run failed: {e}")
