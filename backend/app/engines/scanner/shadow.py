"""Daily SHADOW scan + persist (SCANNER-V1, P2).

Runs every approved equity template through coarse+score+structure-levels and
writes the top watch-only candidates to email_signals_history tagged
`shadow=true` + `matched_strategy=<key>`. This starts the forward-test clock:
the existing resolver (scanner._resolve_email_signal_outcomes) scores each row
against real daily candles (target hit=win / stop hit=loss / 5-day expire), so
per-template stats accumulate toward the promotion gate — WITHOUT any email,
broker order, or change to what the user sees (shadow rows are excluded from the
user-facing /history + the real daily pick uses asset_type='options').

Rate discipline: one grouped-daily snapshot for all templates; per-symbol 1-min
confirmation bars fetched at most once per unique ticker, capped at `max_unique`.
"""
from __future__ import annotations

import os
import json
from loguru import logger

_SHADOW_COLS = [
    "matched_strategy text",
    "shadow boolean DEFAULT false",
    "rr numeric",
    "projected_move_pct numeric",
    "why_selected text",
    "instrument_type text",
]


async def _ensure_columns(db):
    from sqlalchemy import text as _t
    for col in _SHADOW_COLS:
        await db.execute(_t(f"ALTER TABLE email_signals_history ADD COLUMN IF NOT EXISTS {col}"))
    await db.commit()


async def run_shadow_scan(db, *, top_n_per_template: int = 3, max_unique: int = 40,
                          confirm: bool = True) -> dict:
    """Score every approved equity template, persist top watch-only candidates as
    shadow rows. Returns a summary. Persist-only — never emits or trades."""
    from app.engines.options.momentum_scanner import _fetch_market_snapshot
    from app.engines.options.theta_scanner import _get_8k_catalyst
    from app.engines.options.premarket_scheduler import _polygon_1min_bars, _today_et_date_str
    from app.engines.scanner.definitions import equity_templates
    from app.engines.scanner.funnel import _coarse
    from app.engines.scanner.scoring import score_candidate
    from app.engines.scanner.levels import compute_levels
    from sqlalchemy import text as _t

    await _ensure_columns(db)
    templates = equity_templates()
    rows = await _fetch_market_snapshot() or []

    # Stage 1+2: coarse + score per template (no network; catalyst added later)
    per_tpl = []          # (tpl, [cand, ...]) top-N
    need = {}             # ticker -> best score seen (to pick unique set)
    for tpl in templates:
        cands = [c for c in (_coarse(tpl, r) for r in rows) if c]
        for c in cands:
            sb = score_candidate(c, atr_min_pct=tpl.atr_min_pct, atr_max_pct=tpl.atr_max_pct)
            c["score"] = sb.total
            c["why"] = sb.why()
        cands.sort(key=lambda x: x["score"], reverse=True)
        top = [c for c in cands if c["score"] >= tpl.min_score_consider][:top_n_per_template]
        per_tpl.append((tpl, top))
        for c in top:
            need[c["ticker"]] = max(need.get(c["ticker"], 0.0), c["score"])
        logger.info(f"[shadow] {tpl.key}: coarse={len(cands)} top={len(top)}")

    # cap unique tickers by best score — bounds per-symbol Polygon calls
    allowed = {tk for tk, _ in sorted(need.items(), key=lambda kv: kv[1], reverse=True)[:max_unique]}
    date_et = _today_et_date_str()
    bars_cache, cat_cache = {}, {}
    for tk in allowed:
        if confirm:
            try:
                bars_cache[tk] = await _polygon_1min_bars(tk, date_et)
            except Exception:
                bars_cache[tk] = None
        try:
            cat_cache[tk] = await _get_8k_catalyst(db, tk)   # (weight, reason)
        except Exception:
            cat_cache[tk] = (1.0, None)

    persisted, skipped, would_confirm_n = 0, 0, 0
    for tpl, top in per_tpl:
        for c in top:
            tk = c["ticker"]
            if tk not in allowed:
                continue
            lv = compute_levels("long", c["price"], bars_cache.get(tk),
                                rr=tpl.levels.rr_ratio, atr_stop_mult=tpl.levels.atr_stop_mult)
            if not lv.ok:
                skipped += 1
                continue
            cat_w, cat_reason = cat_cache.get(tk, (1.0, None))
            would_confirm = c["score"] >= tpl.min_score_confirm
            would_confirm_n += int(would_confirm)
            # dedup per (template, ticker, ET-day) — a ticker may legitimately
            # appear under multiple templates (separate forward-test cohorts).
            dup = (await db.execute(_t(
                "SELECT 1 FROM email_signals_history WHERE ticker=:tk AND matched_strategy=:ms "
                "AND picked_at::date = (NOW() AT TIME ZONE 'America/New_York')::date LIMIT 1"
            ), {"tk": tk, "ms": tpl.key})).first()
            if dup:
                continue
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
                "tv": int(c.get("today_vol", 0) or 0), "sc": c["score"],
                "cr": cat_reason, "qr": json.dumps(c.get("why", [])),
                "sr": lv.stop_reason, "tr": lv.target_reason, "ms": tpl.key,
                "rr": lv.rr, "pm": lv.projected_move_pct, "wy": json.dumps(c.get("why", [])),
            })
            persisted += 1
    await db.commit()
    summary = {"templates": len(templates), "universe": len(rows),
               "unique_tickers": len(allowed), "persisted": persisted,
               "skipped_no_levels": skipped, "would_confirm": would_confirm_n}
    logger.info(f"[shadow] done {summary}")
    return summary


# ── scheduler hook — once per ET trading day, persist-only, fully isolated ──
async def _check_and_run_shadow_scan() -> None:
    """Called each scheduler loop; runs the shadow scan exactly once per trading
    day (Redis SETNX latch) once past ~09:45 ET. Gated by SCANNER_SHADOW_ENABLED
    (default on). Any failure is swallowed — it can never affect the real scan."""
    if os.environ.get("SCANNER_SHADOW_ENABLED", "1") != "1":
        return
    from datetime import datetime as _dt, timezone as _tz
    try:
        import zoneinfo
        et = _dt.now(_tz.utc).astimezone(zoneinfo.ZoneInfo("America/New_York"))
    except Exception:
        return
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
        if not _r.set(f"theta:shadow:{today_key}", "running", ex=36 * 3600, nx=True):
            return  # already ran today (or another worker is running it)
    except Exception:
        return  # no redis latch → skip rather than risk duplicate daily runs
    try:
        from app.database import async_session_factory as _asf
        logger.info(f"[shadow] daily run start {et.strftime('%H:%M ET')} ({today_key})")
        async with _asf() as db:
            await run_shadow_scan(db)
    except Exception as e:
        logger.error(f"[shadow] daily run failed: {e}")
