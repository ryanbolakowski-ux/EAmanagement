"""Theta Scanner — STT-style single-pick premarket scanner."""
import os, math, json
from datetime import datetime, timezone
from typing import Optional
from loguru import logger

_NOPICK_STATE: dict = {"last": None}
from sqlalchemy import text

_CATALYST_WEIGHTS = {
    "1.01": 2.0, "2.02": 1.3, "7.01": 1.4, "8.01": 1.5, "5.02": 1.2,
}


async def _get_8k_catalyst(db, ticker: str):
    try:
        r = (await db.execute(text("""
            SELECT item_codes, filed_at FROM edgar_filings
             WHERE ticker = :t AND filed_at > NOW() - INTERVAL '36 hours'
             ORDER BY filed_at DESC LIMIT 1
        """), {"t": ticker})).first()
        if not r: return 1.0, ""
        codes = r.item_codes or "[]"
        try: codes = json.loads(codes) if isinstance(codes, str) else codes
        except Exception: codes = []
        max_w, best = 1.0, ""
        for c in codes:
            w = _CATALYST_WEIGHTS.get(c, 1.0)
            if w > max_w: max_w, best = w, f"8-K item {c}"
        return max_w, best
    except Exception:
        return 1.0, ""


# ── Quality filters (added 2026-06-09) ──────────────────────────────────────
# Reuse the battle-tested Polygon 1-min helpers already living in
# premarket_scheduler so we have ONE implementation of VWAP / pre-mkt windowing.
def _session_vwap(bars_1m: list) -> Optional[float]:
    """Session VWAP = Sum(typical_price * vol) / Sum(vol) across all available
    1-min bars (pre-market + any RTH so far). typical = (h+l+c)/3, or Polygon's
    per-minute 'vw' when present. Returns None if no volume."""
    num = 0.0
    den = 0.0
    for b in bars_1m or []:
        try:
            v = float(b.get("v", 0) or 0)
            if v <= 0:
                continue
            vw = b.get("vw")
            if vw is None:
                h = float(b.get("h", 0) or 0)
                l = float(b.get("l", 0) or 0)
                c = float(b.get("c", 0) or 0)
                vw = (h + l + c) / 3.0
            else:
                vw = float(vw)
            if vw <= 0:
                continue
            num += vw * v
            den += v
        except Exception:
            continue
    return (num / den) if den > 0 else None


def _premarket_dollar_volume(bars_1m: list) -> float:
    """Sum(close * volume) over the 04:00-09:29 ET pre-market 1-min bars."""
    from app.engines.options.premarket_scheduler import _bar_is_premarket_et
    total = 0.0
    for b in bars_1m or []:
        try:
            if not _bar_is_premarket_et(int(b.get("t", 0))):
                continue
            c = float(b.get("c", 0) or 0)
            v = float(b.get("v", 0) or 0)
            if c > 0 and v > 0:
                total += c * v
        except Exception:
            continue
    return total


def _last3_higher_highs(bars_1m: list) -> bool:
    """Anti-fade: the last 3 (non-empty) 1-min bars make strictly higher highs.
    Returns False if fewer than 3 usable bars (caller treats as continuation
    fail → watch-only, not a hard reject)."""
    highs = []
    for b in bars_1m or []:
        try:
            h = float(b.get("h", 0) or 0)
            if h > 0:
                highs.append(h)
        except Exception:
            continue
    if len(highs) < 3:
        return False
    a, bb, c = highs[-3], highs[-2], highs[-1]
    return c > bb > a


async def _apply_quality_filters(db, c: dict) -> tuple:
    """Run the new long-side quality gate on a candidate dict (must have
    'ticker' and 'price'). Returns (verdict, reasons) where verdict is one of
    'accept' | 'watch' | 'reject' and reasons is list[str].

    Filters:
      1. VWAP — reject if price < session VWAP (long below VWAP).
      2. Pre-mkt liquidity — reject if pre-mkt $-vol < $1M.
      3. Continuation — last 3 1-min bars must make higher highs.
      4. Anti-overextension — reject if price > 8% above VWAP.
    Degrades gracefully: if Polygon bars are unavailable for the ticker we SKIP
    the bar-dependent filters (log it) rather than crashing the scan. VWAP /
    continuation failures are SOFT (watch-only); liquidity / overextension are
    HARD rejects.
    """
    from app.engines.options.premarket_scheduler import (
        _polygon_1min_bars, _today_et_date_str,
    )
    ticker = c["ticker"]
    price = float(c["price"])
    reasons: list[str] = []
    # Base reasons that always apply (passed gap+vol+score upstream).
    reasons.append(f"gap {c.get('gap_pct', 0):.0f}%")
    reasons.append(f"rel-vol {c.get('rel_vol', 0)}x")
    if c.get("catalyst_reason") and c["catalyst_reason"] != "high rel-vol gap":
        reasons.append(f"catalyst: {c['catalyst_reason']}")

    date_et = _today_et_date_str()
    # REALTIME-FEED-FMP: with REALTIME_FEED=fmp, FMP's 1-min endpoint is
    # REAL-TIME for ANY candidate ticker (no subscription needed) — prefer it
    # so the 09:30-09:35 candles are readable AT 09:35 for every candidate.
    # Same discipline as the store path below: the helper returns [] on any
    # failure / other provider / flag off (it never raises), and we fall back
    # to the (15-min-delayed) Polygon REST aggs — flag-off behavior stays
    # byte-identical to today.
    bars_1m = None
    try:
        from app.engines.data_feeds.realtime_feed import get_ondemand_intraday_bars
        bars_1m = await get_ondemand_intraday_bars(ticker, date_et=date_et)
        if bars_1m:
            logger.info(f"[ThetaScanner] {ticker}: confirmation bars source=fmp ({len(bars_1m)} live 1-min bars)")
    except Exception as _fmp_exc:  # helper never raises — belt and braces
        logger.info(f"[ThetaScanner] {ticker}: fmp on-demand bars skipped ({type(_fmp_exc).__name__}: {_fmp_exc})")
        bars_1m = None
    if not bars_1m:
        try:
            bars_1m = await _polygon_1min_bars(ticker, date_et)
        except Exception as e:
            logger.info(f"[ThetaScanner] {ticker}: 1-min bar fetch errored ({type(e).__name__}: {e}) — skipping bar filters")
            bars_1m = None

    # REALTIME-FEED-V1: merge seconds-fresh ws minute bars from the in-process
    # store over the (15-min-delayed) REST aggs. At 09:35 the delayed REST
    # often has NOTHING for the open yet — the store makes the 09:30-09:35
    # candles readable the moment they close, so confirmation happens at 09:35
    # instead of ~09:50 (and a live store alone rescues the "no Polygon
    # intraday bars" watch-only downgrade below). Flag-gated: with
    # REALTIME_FEED off (the default) get_fresh_bars() returns [] and bars_1m
    # stays byte-identical to today's REST-only value.
    try:
        from app.engines.data_feeds.realtime_feed import get_fresh_bars, request_symbols
        live_bars = get_fresh_bars(ticker)
        if live_bars:
            # Store bars share the REST-aggs dict shape ('t','o','h','l','c',
            # 'v','vw'), so the merge is a dedupe-by-minute: live wins on the
            # same start-ms (its bar is at least as complete as the REST one).
            _by_t = {int(b.get("t") or 0): b for b in (bars_1m or [])}
            for _b in live_bars:
                _by_t[int(_b.get("t") or 0)] = _b
            _by_t.pop(0, None)  # drop anything with an unusable timestamp
            bars_1m = [_by_t[k] for k in sorted(_by_t)]
            logger.info(f"[ThetaScanner] {ticker}: merged {len(live_bars)} realtime store bars into intraday set")
        else:
            # Not streaming this ticker yet — subscribe now so the NEXT scan
            # tick reads it live. No-op when the flag is off.
            request_symbols([ticker])
    except Exception as _rt_exc:
        logger.info(f"[ThetaScanner] {ticker}: realtime store merge skipped ({type(_rt_exc).__name__}: {_rt_exc})")

    if not bars_1m:
        # No intraday data pre-market for this ticker — degrade gracefully.
        # THETA-UNCONFIRMED-WATCH-V1: no intraday data -> we CANNOT confirm
        # liquidity / VWAP / continuation, so this is NOT a tradeable pick.
        # Downgrade to WATCH-ONLY ("no trade > unconfirmed pick"), kept visible.
        logger.info(f"[ThetaScanner] {ticker}: no Polygon intraday bars — UNCONFIRMED, downgraded to watch-only")
        reasons.append("unconfirmed: no intraday bars (watch-only)")
        return "watch", reasons

    soft_fail = False  # VWAP-below or continuation fail → watch-only

    # 2. Liquidity: SESSION-TO-DATE $-vol (premarket OR live session, whichever is
    #    larger) — NOT premarket-only, which was rejecting genuinely liquid large-caps
    #    (e.g. TECH/AYI) just because they are quiet PRE-market. The coarse filter
    #    already requires >=$20M avg day $-vol, so this is a sanity floor.
    # Liquidity is ALREADY gated by the coarse filter (>=$20M avg day $-vol) +
    # leveraged-ETF exclusion, so the 1-min-bar $-vol is NO LONGER a hard reject — it
    # was throwing out liquid large-caps (TECH/AYI/ASND) that are quiet PRE-market and
    # whose RTH volume is not in the (15-min-delayed) bars yet. Only a truly dead tape
    # (<$250k session-to-date) downgrades to watch-only.
    pm_dv = _premarket_dollar_volume(bars_1m)
    try:
        sess_dv = sum(float(b.get("c") or 0) * float(b.get("v") or 0) for b in bars_1m)
    except Exception:
        sess_dv = 0.0
    liq_dv = max(pm_dv, sess_dv)
    if liq_dv < 250_000:
        logger.info(f"[ThetaScanner] {ticker}: intraday $-vol ${liq_dv:,.0f} < $250k — thin, WATCH-ONLY")
        soft_fail = True
    reasons.append(f"$-vol ${liq_dv/1e6:.2f}M")

    # 1 + 4. VWAP-relative checks. Below VWAP -> watch-only (long bias unconfirmed);
    #    >7% above VWAP -> overextended reject (chasing). (Widened 5%->7% for momentum.)
    vwap = _session_vwap(bars_1m)
    if vwap and vwap > 0:
        dist_pct = (price - vwap) / vwap * 100.0
        if dist_pct > 7.0:
            logger.info(f"[ThetaScanner] reject {ticker}: price ${price:.2f} is {dist_pct:.1f}% above VWAP ${vwap:.2f} (>7% overextended)")
            return "reject", reasons
        if price < vwap:
            logger.info(f"[ThetaScanner] {ticker}: price ${price:.2f} below VWAP ${vwap:.2f} (long-below-VWAP) — watch-only")
            soft_fail = True
            reasons.append(f"below VWAP ${vwap:.2f}")
        else:
            reasons.append(f"above VWAP (+{dist_pct:.1f}%)")
    else:
        logger.info(f"[ThetaScanner] {ticker}: VWAP unavailable — skipping VWAP filter (graceful)")
        reasons.append("VWAP n/a")

    # 3. Continuation is a SCORE NOTE, not a gate. Requiring 3 consecutive 1-min
    #    higher highs at the scan instant rejected almost everything (intraday noise),
    #    so a clean liquid above-VWAP setup was being demoted to watch-only. Note it
    #    for context; do NOT downgrade on its absence.
    if _last3_higher_highs(bars_1m):
        reasons.append("HH x3")
    else:
        reasons.append("consolidating")

    return ("watch" if soft_fail else "accept"), reasons



_LAST_SCAN_DIAG = {"last": None}


async def find_best_pick_via_funnel(db):
    """LIVE daily pick from the PROMOTED multi-strategy templates (SCANNER-V1).

    Broad, liquidity-aware candidate sourcing (NOT premarket-gap-only, which was
    biased toward illiquid pumps) across every enabled template, then the SAME
    quality gate the legacy path uses (_apply_quality_filters: premkt liquidity /
    VWAP / continuation) + structure-based levels (compute_levels). Returns a
    legacy-shaped pick dict (so emit_theta_pick is unchanged) or None.
    """
    from app.engines.options.momentum_scanner import _fetch_market_snapshot
    from app.engines.scanner.definitions import enabled_templates
    from app.engines.scanner.funnel import _coarse
    from app.engines.scanner.scoring import score_candidate

    tpls = [t for t in enabled_templates() if not t.options.eligible]
    if not tpls:
        return None
    rows = await _fetch_market_snapshot() or []

    # coarse + score across all promoted templates; keep best (score, template) per ticker
    best_by_tkr = {}
    for tpl in tpls:
        for r in rows:
            c = _coarse(tpl, r)
            if not c:
                continue
            sb = score_candidate(c, atr_min_pct=tpl.atr_min_pct, atr_max_pct=tpl.atr_max_pct)
            c["score"] = sb.total
            c["matched_strategy"] = tpl.key
            c["_rr"] = tpl.levels.rr_ratio
            prev = best_by_tkr.get(c["ticker"])
            if prev is None or c["score"] > prev["score"]:
                best_by_tkr[c["ticker"]] = c
    cands = sorted(best_by_tkr.values(), key=lambda x: x["score"], reverse=True)
    if not cands:
        _NOPICK_STATE["last"] = {"ticker": None, "score": 0,
            "reason": "no liquid candidate matched a promoted template today"}
        return None

    for c in cands[:12]:
        try:
            _cw, _cr = await _get_8k_catalyst(db, c["ticker"])
            c["catalyst_reason"] = _cr or "multi-strategy match"
        except Exception:
            c["catalyst_reason"] = "multi-strategy match"

    # walk best-first through the SAME quality gate the legacy path uses
    best = None
    best_watch = None
    _evaluated = []
    for c in cands[:12]:
        try:
            verdict, reasons = await _apply_quality_filters(db, c)
        except Exception as e:
            logger.error(f"[stock-scanner] funnel quality-filter crashed {c['ticker']}: {e}")
            continue
        c["quality_reasons"] = reasons
        _evaluated.append({"ticker": c["ticker"], "score": round(float(c["score"]), 1),
                           "gap_pct": c.get("gap_pct"), "rel_vol": c.get("rel_vol"),
                           "price": c.get("price"), "via": c.get("matched_strategy"),
                           "verdict": verdict, "reasons": reasons})
        if verdict == "accept":
            c["watch_only"] = False
            best = c
            break
        if verdict == "watch" and best_watch is None:
            c["watch_only"] = True
            best_watch = c
    if best is None:
        best = best_watch
    if best is None:
        _top = cands[0]
        _qr = _top.get("quality_reasons") or ["no intraday confirmation"]
        _NOPICK_STATE["last"] = {"ticker": _top["ticker"], "score": round(float(_top["score"]), 2),
            "reason": (f"top liquid candidate {_top['ticker']} ({_top['score']:.0f}) failed quality: "
                       + "; ".join(str(r) for r in _qr))}
        _LAST_SCAN_DIAG["last"] = {"universe": len(rows), "candidates": len(cands),
            "evaluated": _evaluated, "pick": None,
            "no_trade_reason": _NOPICK_STATE["last"]["reason"]}
        logger.info("[stock-scanner] funnel: no candidate cleared the quality gate")
        return None

    best["entry"] = best["price"]
    from app.engines.options.premarket_scheduler import _polygon_1min_bars, _today_et_date_str
    from app.engines.scanner.levels import compute_levels
    try:
        _bars = await _polygon_1min_bars(best["ticker"], _today_et_date_str())
    except Exception:
        _bars = None
    _lv = compute_levels("long", float(best["price"]), _bars, rr=float(best.get("_rr", 2.0)))
    best["stop"] = _lv.stop
    best["target"] = _lv.target
    best["projected_move_pct"] = _lv.projected_move_pct
    best["stop_reason"] = _lv.stop_reason
    best["target_reason"] = _lv.target_reason
    best["rr"] = _lv.rr
    best["levels_basis"] = _lv.basis
    best["stop_is_placeholder"] = (_lv.basis == "atr_fallback")
    if not _lv.ok and not best.get("watch_only"):
        best["watch_only"] = True
        best.setdefault("quality_reasons", []).append(
            f"R:R {_lv.rr:.1f} below minimum on available structure — watch-only")
    best["asset_type"] = "options"   # legacy UI/health key for the Theta pick (it is a stock)
    best.setdefault("catalyst_reason", "multi-strategy match")
    best.setdefault("quality_reasons", [])
    best.setdefault("watch_only", False)
    best["alternatives"] = [{"ticker": c["ticker"], "score": round(c["score"], 1),
                             "gap_pct": round(c.get("gap_pct", 0), 1)} for c in cands[1:6]]
    _NOPICK_STATE["last"] = None
    _LAST_SCAN_DIAG["last"] = {"universe": len(rows), "candidates": len(cands),
        "evaluated": _evaluated, "pick": best["ticker"], "no_trade_reason": None}
    logger.info(
        f"[stock-scanner] FUNNEL PICK {best['ticker']} via {best.get('matched_strategy')} "
        f"score={best['score']:.0f} entry=${best['price']:.2f} stop={best['stop']} "
        f"({best['stop_reason']}) target={best['target']} rr={best['rr']} "
        f"basis={best['levels_basis']} watch_only={best['watch_only']}")
    try:
        import redis.asyncio as _r
        import json as _j
        from datetime import date as _d
        _redis = _r.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"), decode_responses=True)
        payload = {k: v for k, v in best.items() if k not in ("_rr",)}
        payload["picked_at"] = datetime.now(timezone.utc).isoformat()
        await _redis.setex(f"theta:lastpick:{_d.today().isoformat()}", 36 * 3600, _j.dumps(payload, default=str))
        await _redis.setex("theta:lastpick:latest", 36 * 3600, _j.dumps(payload, default=str))
    except Exception as e:
        logger.warning(f"[stock-scanner] funnel lastpick redis write failed: {e}")
    return best


async def find_best_premarket_pick(db) -> Optional[dict]:
    # SCANNER-V1: prefer the promoted multi-strategy funnel (broad, liquidity-
    # aware, structure-vetted) over the legacy premarket-gapper scan. Falls back
    # to legacy below if nothing is promoted or the funnel surfaces no pick.
    try:
        from app.engines.scanner.definitions import enabled_templates as _enabled
        if _enabled():
            _fp = await find_best_pick_via_funnel(db)
            if _fp:
                return _fp
    except Exception as _fe:
        logger.warning(f"[stock-scanner] funnel path failed, using legacy: {_fe}")
    from app.engines.options.momentum_scanner import _fetch_market_snapshot
    rows = await _fetch_market_snapshot()
    if not rows:
        logger.warning("[ThetaScanner] scanner returned 0 rows")
        return None
    candidates = []
    for r in rows:
        try:
            price = float(r.get("day", {}).get("c") or 0)
            today_vol = int(r.get("day", {}).get("v") or 0)
            prev_close = float(r.get("prevDay", {}).get("c") or 0)
            prev_vol = int(r.get("prevDay", {}).get("v") or 0)
            ticker = r.get("ticker", "")
            if not ticker or price <= 0 or prev_close <= 0: continue
            # Skip warrants (W/WS), units (U), rights (R), and preferreds (P)
            # These have thin liquidity, unusual mechanics, and rarely trade options
            t_upper = ticker.upper()
            if (t_upper.endswith("W") or t_upper.endswith("WS") or
                t_upper.endswith(".U") or t_upper.endswith("/U") or
                t_upper.endswith(".R") or
                "." in t_upper or "/" in t_upper):
                continue
            gap_pct = (price - prev_close) / prev_close * 100.0
            if not (3.0 <= gap_pct <= 30.0): continue  # broadened from 5-25 (was pump-biased)
            if price < 10 or price > 200: continue  # raised floor to skip micro-caps (was 2)
            if today_vol * price < 5_000_000: continue
            # prev_vol == 0 means NO completed-session baseline (recent IPO, or
            # an FMP mover missing from the Polygon prev map — fmp_universe
            # emits prevDay.v=0 by its never-fabricate contract). The old
            # `prev_vol > 0 and ...` skipped the surge gate entirely for such
            # rows and max(prev_vol, 1) then granted the max rel_vol score
            # multiplier — a fabricated hit. No baseline → no candidate.
            if prev_vol <= 0: continue
            if today_vol / prev_vol < 2.5: continue
            rel_vol = today_vol / prev_vol
            cat_w, cat_reason = await _get_8k_catalyst(db, ticker)
            score = gap_pct * math.log(max(today_vol, 1)) * cat_w * min(rel_vol, 10) / 100
            candidates.append({
                "ticker": ticker, "price": price, "gap_pct": gap_pct,
                "today_vol": today_vol, "rel_vol": round(rel_vol, 2),
                "catalyst_weight": cat_w,
                "catalyst_reason": cat_reason or "high rel-vol gap",
                "score": round(score, 2),
            })
        except Exception:
            continue
    if not candidates:
        logger.info("[ThetaScanner] no candidate passed quality filters")
        _NOPICK_STATE["last"] = {"ticker": None, "score": None,
            "reason": "no gapper met the universe filters today"}
        return None
    candidates.sort(key=lambda c: c["score"], reverse=True)
    # Per-candidate visibility (every candidate, not just the winner) so we can
    # audit why a setup did/didn't make the cut on any given day.
    for c in candidates[:8]:
        logger.info(
            f"[stock-scanner] candidate {c['ticker']} score={c['score']:.2f} "
            f"gap={c['gap_pct']:.1f}% rel_vol={c['rel_vol']} "
            f"catalyst={c.get('catalyst_reason') or 'none'}"
        )
    # MIN_SCORE floor (added 2026-06-05): only fire if the top candidate clears
    # the quality bar. After 4 losing micro-cap stop-outs in 5 days, the bar
    # is non-negotiable. Reject sub-floor days entirely.
    MIN_SCORE = 15.0      # floor to CONSIDER (raised back to 15 on 2026-06-16: CBRL@14.55 fired + lost)
    CONFIRM_SCORE = 20.0  # >= this = confirmed entry; MIN_SCORE..CONFIRM_SCORE = WATCH ONLY (raised 2026-06-16)
    if candidates[0]["score"] < MIN_SCORE:
        logger.info(
            f"[stock-scanner] no pick \u2014 best candidate {candidates[0]['ticker']} "
            f"score={candidates[0]['score']:.2f} below MIN_SCORE={MIN_SCORE}"
        )
        _NOPICK_STATE["last"] = {"ticker": candidates[0]["ticker"],
            "score": round(float(candidates[0]["score"]), 2),
            "reason": f"best candidate scored {candidates[0]['score']:.1f}, below the {MIN_SCORE:.0f} minimum"}
        for c in candidates[:5]:
            logger.info(
                f"[stock-scanner] rejected candidate: {c['ticker']} "
                f"score={c['score']:.2f} gap={c['gap_pct']:.1f}% rel_vol={c['rel_vol']}"
            )
        return None

    # ── Quality gate (added 2026-06-09) ──────────────────────────────────
    # Walk candidates best-score-first. A "clean" candidate (accept) wins
    # immediately. Otherwise remember the best watch-only candidate so that on
    # a day with no clean setup we still surface the strongest gapper, flagged
    # watch_only=True so the email reads "WATCH ONLY — not a trade."
    best = None
    best_watch = None
    for c in candidates:
        if c["score"] < MIN_SCORE:
            break  # remaining are weaker; below floor
        try:
            verdict, reasons = await _apply_quality_filters(db, c)
        except Exception as e:
            logger.error(f"[ThetaScanner] quality-filter crashed for {c['ticker']}: {e} — skipping candidate")
            continue
        c["quality_reasons"] = reasons
        if verdict == "accept":
            # Cleared quality filters. If the score is in the MIN..CONFIRM
            # band it's a lower-conviction near-miss -> WATCH ONLY (not a
            # hard entry). >= CONFIRM_SCORE -> confirmed entry.
            c["watch_only"] = c["score"] < CONFIRM_SCORE
            best = c
            break
        if verdict == "watch" and best_watch is None:
            c["watch_only"] = True
            best_watch = c
        # verdict == "reject" → drop silently (already logged)

    if best is None:
        if best_watch is not None:
            best = best_watch
            logger.info(
                f"[stock-scanner] no CLEAN pick \u2014 surfacing WATCH-ONLY {best['ticker']} "
                f"score={best['score']:.2f} reasons={best.get('quality_reasons')}"
            )
        else:
            logger.info("[stock-scanner] no pick \u2014 all candidates rejected by quality filters")
            _top = candidates[0]
            _qr = _top.get("quality_reasons") or []
            _NOPICK_STATE["last"] = {"ticker": _top["ticker"],
                "score": round(float(_top["score"]), 2),
                "reason": (f"top candidate {_top['ticker']} ({_top['score']:.1f}) failed quality: "
                           + ("; ".join(str(r) for r in _qr) if _qr else "did not clear the filters"))}
            return None

    _NOPICK_STATE["last"] = None  # a pick fired today
    best.setdefault("watch_only", False)
    best.setdefault("quality_reasons", [])
    best["entry"] = best["price"]
    # SCANNER-LEVELS-V1: structure-based stop/target replaces the old -3%/+10%
    # placeholder (whose fixed geometry produced an artificial win rate). Levels
    # come from real session/swing structure + a measured-move target; ATR is the
    # fallback only when no clean structure sits in range. A sub-minimum-R:R
    # result downgrades the pick to watch-only instead of forcing a bad level.
    try:
        from app.engines.options.premarket_scheduler import _polygon_1min_bars, _today_et_date_str
        from app.engines.scanner.levels import compute_levels
        _bars = await _polygon_1min_bars(best["ticker"], _today_et_date_str())
        _lv = compute_levels("long", float(best["price"]), _bars, rr=2.5)
        best["stop"] = _lv.stop
        best["target"] = _lv.target
        best["projected_move_pct"] = _lv.projected_move_pct
        best["stop_reason"] = _lv.stop_reason
        best["target_reason"] = _lv.target_reason
        best["rr"] = _lv.rr
        best["levels_basis"] = _lv.basis
        best["stop_is_placeholder"] = (_lv.basis == "atr_fallback")
        if not _lv.ok and not best.get("watch_only"):
            best["watch_only"] = True
            best.setdefault("quality_reasons", []).append(
                f"R:R {_lv.rr:.1f} below minimum on available structure \u2014 watch-only")
        logger.info(
            f"[stock-scanner] levels {best['ticker']} stop={_lv.stop} ({_lv.stop_reason}) "
            f"target={_lv.target} ({_lv.target_reason}) rr={_lv.rr} "
            f"stop%={_lv.detail.get('stop_pct')} basis={_lv.basis} ok={_lv.ok}")
    except Exception as _le:
        logger.warning(f"[ThetaScanner] structure levels failed for {best.get('ticker')}: {_le}")
        from app.engines.scanner.levels import compute_levels as _cl
        _lv = _cl("long", float(best["price"]), None, rr=2.5)
        best["stop"] = _lv.stop
        best["target"] = _lv.target
        best["projected_move_pct"] = _lv.projected_move_pct
        best["stop_reason"] = _lv.stop_reason
        best["target_reason"] = _lv.target_reason
        best["rr"] = _lv.rr
        best["levels_basis"] = "atr_fallback"
        best["stop_is_placeholder"] = True
    best["alternatives"] = [{"ticker": c["ticker"], "score": c["score"],
                              "gap_pct": round(c["gap_pct"], 1)} for c in candidates[1:6]]
    logger.info(f"[ThetaScanner] PICK: {best['ticker']} @ ${best['price']:.2f} score={best['score']} gap={best['gap_pct']:.1f}% vol={best['today_vol']:,} catalyst={best['catalyst_reason']}")
    # Persist to Redis so the frontend "Today's Pick" widget can read it
    try:
        import redis.asyncio as _r
        import json as _j
        from datetime import date as _d
        _redis = _r.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"), decode_responses=True)
        payload = dict(best)
        payload["picked_at"] = datetime.now(timezone.utc).isoformat()
        await _redis.setex(f"theta:lastpick:{_d.today().isoformat()}", 36*3600, _j.dumps(payload))
        await _redis.setex("theta:lastpick:latest", 36*3600, _j.dumps(payload))
    except Exception as e:
        logger.warning(f"[ThetaScanner] could not persist pick to redis: {e}")
    return best


async def emit_theta_pick(db, user, pick: dict) -> bool:
    from app.services.email import _send, _send_tracked
    qty = max(1, int(1000 / pick["price"]))
    _watch = bool(pick.get("watch_only"))
    _qr = pick.get("quality_reasons") or []
    subject = f"🎯 Saro — Today's Pick: {pick['ticker']} +{pick['projected_move_pct']:.0f}% target"
    if _watch:
        subject = f"🎯 Saro: 👀 WATCH ONLY — {pick['ticker']} (no clean setup today)"
    alt_html = ""
    if pick.get("alternatives"):
        alt_html = "<p style='font-size:11px;color:#94a3b8;'>Runners-up: " + ", ".join(
            f"{a['ticker']} (gap {a['gap_pct']}%)" for a in pick["alternatives"][:3]) + "</p>"
    watch_banner = (
        "<div style=\"background:#fef3c7;border:1px solid #f59e0b;color:#92400e;padding:10px 12px;border-radius:8px;font-size:13px;font-weight:700;margin:0 0 14px;\">\u26a0\ufe0f WATCH ONLY \u2014 not a trade. No setup cleared the VWAP / continuation filters today; this is the strongest gapper for context only.</div>"
        if _watch else ""
    )
    reasons_html = (
        "<p style=\"font-size:11px;color:#475569;margin:0 0 14px;\">Quality: "
        + " \u00b7 ".join(_qr) + "</p>"
    ) if _qr else ""
    # ── Annotated trade-chart PNG (best-effort) ──────────────────────────
    # Render the TradingView-style position chart from the same pre-market
    # 1-min Polygon bars the quality filters use (_polygon_1min_bars). Attach
    # it inline (<img src="cid:tradechart">) and stash base64 for the Email
    # Signals history. Even a watch_only pick gets a chart (informative); the
    # banner already says WATCH ONLY. Any failure -> no-chart email.
    # Level reasons shown next to the stop/target price (never blank).
    stop_reason = pick.get("stop_reason") or "strategy stop"
    target_reason = pick.get("target_reason") or "strategy target"
    _chart_png = None
    _chart_b64 = None
    _chart_img_html = ""
    try:
        from app.engines.options.premarket_scheduler import _polygon_1min_bars, _today_et_date_str
        from app.services.trade_chart import generate_trade_chart
        import pandas as _pd_ch
        _raw_bars = await _polygon_1min_bars(pick["ticker"], _today_et_date_str())
        _bars_df = None
        if _raw_bars:
            _bars_df = _pd_ch.DataFrame([{
                "timestamp": _pd_ch.to_datetime(int(b.get("t", 0)), unit="ms", utc=True),
                "open": float(b.get("o", 0) or 0), "high": float(b.get("h", 0) or 0),
                "low": float(b.get("l", 0) or 0), "close": float(b.get("c", 0) or 0),
                "volume": int(b.get("v", 0) or 0),
            } for b in _raw_bars if float(b.get("c", 0) or 0) > 0])
        # Infer human-readable level reasons from the same pre-market bars
        # (never blank; falls back to strategy stop / strategy target).
        try:
            from app.engines.level_reasons import infer_stop_target_reasons as _infer_lr
            _reasons = _infer_lr(
                direction="long", entry=float(pick["entry"]),
                stop=float(pick["stop"]), target=float(pick["target"]),
                bars_df=_bars_df, instrument=pick["ticker"],
            )
            stop_reason = pick.get("stop_reason") or _reasons.get("stop_reason") or stop_reason
            target_reason = pick.get("target_reason") or _reasons.get("target_reason") or target_reason
        except Exception as _lr_e:
            logger.warning(f"[ThetaScanner] reason inference errored {pick.get('ticker')}: {type(_lr_e).__name__}: {_lr_e}")
        _chart_png = generate_trade_chart(
            symbol=pick["ticker"], timeframe="1m", bars_df=_bars_df,
            entry=float(pick["entry"]), stop=float(pick["stop"]),
            target=float(pick["target"]), direction="long", key_levels=None,
            stop_reason=stop_reason, target_reason=target_reason,
        )
    except Exception as _ch_e:
        logger.warning(f"[ThetaScanner] chart gen errored {pick.get('ticker')}: {type(_ch_e).__name__}: {_ch_e}")
        _chart_png = None
    if _chart_png:
        import base64 as _b64_ch
        _chart_b64 = _b64_ch.b64encode(_chart_png).decode()
        _chart_img_html = (
            '<img src="cid:tradechart" alt="trade setup" '
            'style="display:block;width:100%;max-width:520px;border-radius:12px;'
            'border:1px solid #e2e8f0;margin:14px 0;"/>'
        )
    else:
        logger.info(f"[ThetaScanner] chart skipped (invalid geometry) {pick.get('ticker')} e={pick.get('entry')} s={pick.get('stop')} t={pick.get('target')}")
    html = f"""<div style="font-family:-apple-system,sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#0f172a;">
      <h1 style="margin:0 0 8px;color:#7c3aed;">🎯 Saro — Today's Pick</h1>
      {watch_banner}{reasons_html}
      <p style="color:#64748b;font-size:12px;margin:0 0 20px;">STT-style single highest-conviction setup for {datetime.now(timezone.utc).date()}</p>
      <table style="width:100%;border-collapse:collapse;font-size:14px;">
        <tr><td style="padding:8px;color:#475569;">Ticker</td><td style="text-align:right;font-weight:700;font-size:18px;">{pick['ticker']}</td></tr>
        <tr><td style="padding:8px;color:#475569;">Entry</td><td style="text-align:right;font-weight:700;">${pick['entry']:.2f}</td></tr>
        <tr><td style="padding:8px;color:#475569;">Stop ({(pick['stop']/pick['entry']-1)*100:+.1f}%)</td><td style="text-align:right;font-weight:700;color:#dc2626;">${pick['stop']:.2f} <span style="color:#94a3b8;font-weight:600;font-size:12px;">({stop_reason})</span></td></tr>
        <tr><td style="padding:8px;color:#475569;">Target ({(pick['target']/pick['entry']-1)*100:+.1f}%)</td><td style="text-align:right;font-weight:700;color:#16a34a;">${pick['target']:.2f} <span style="color:#94a3b8;font-weight:600;font-size:12px;">({target_reason})</span></td></tr>
        <tr><td style="padding:8px;color:#475569;">Gap</td><td style="text-align:right;">+{pick['gap_pct']:.1f}%</td></tr>
        <tr><td style="padding:8px;color:#475569;">Volume vs prior</td><td style="text-align:right;">{pick['rel_vol']}×</td></tr>
        <tr><td style="padding:8px;color:#475569;">Catalyst</td><td style="text-align:right;">{pick['catalyst_reason']}</td></tr>
        <tr><td style="padding:8px;color:#475569;">Score</td><td style="text-align:right;">{pick['score']}</td></tr>
      </table>
      {_chart_img_html}
      <p style="margin:16px 0;font-size:12px;color:#64748b;">Picked by <b>Saro</b> — the daily stock scanner and your sole auto-pick source. All 22 ICT/options strategies remain in your Live Trading library for manual trades.</p>
      {alt_html}
      <p style="font-size:10px;color:#94a3b8;margin-top:24px;">Risk: 10% target is historical median for setups matching this profile. Not a guarantee. Confirm size + stop before adding.</p>
    </div>"""
    ok = _send_tracked(user.email, subject, html, inline_png=_chart_png).get("sent")

    # === Entry timing gate (2026-06-05) ===
    # The blind 15-min auto-execute caused SPRC to enter 2.5h after pick at
    # a price that was ALREADY below the stop. Per the user spec, the
    # broker-fire path is now ROUTED through _execute_stock_pick_with_timing_gate
    # which checks (a) pre-market VWAP + higher-highs between 08:30-09:25 ET,
    # or (b) places a market-on-open order at 09:30+. Stop is computed at
    # order-placement time from the ICT Oracle 5-min opening candle (or the
    # pre-market session low for pre-mkt-confirmed entries) instead of the
    # blanket -3% which was triggering 7%+ losses on micro-caps.
    #
    # Implementation: enqueue the pick to Redis. The scheduler tick
    # (_check_pending_stock_entries) picks it up each cycle and asks the
    # timing gate whether to fire now, wait, or defer.
    try:
        from app.engines.options.premarket_scheduler import _resolve_user_broker
        broker_account_id, trade_mode = await _resolve_user_broker(user.id)
        # WATCH-ONLY never trades: an unconfirmed / watch-only pick is informational
        # only — never queue a live broker entry for it (it can still email).
        if _watch and broker_account_id:
            logger.info(f"[ThetaScanner] {user.email}: watch-only pick — NOT queuing a broker entry")
            broker_account_id = None
        # Honor per-account trading_enabled toggle (TradeSyncer-style)
        if broker_account_id:
            from app.models.user import BrokerAccount
            from sqlalchemy import select as _sel
            _acct = (await db.execute(_sel(BrokerAccount).where(BrokerAccount.id == broker_account_id))).scalar_one_or_none()
            if _acct and not getattr(_acct, "trading_enabled", True):
                logger.info(f"[ThetaScanner] {user.email}: trading_enabled=False on broker account — skipping Tradier fire")
                broker_account_id = None
        if trade_mode == "live" and broker_account_id:
            from datetime import date as _date
            qty = max(1, int(1000 / pick["price"]))  # $1000 position
            entry_payload = {
                "user_id": str(user.id),
                "user_email": user.email,
                "broker_account_id": broker_account_id,
                "ticker": pick["ticker"],
                "direction": "long",
                "qty": qty,
                "pick_price": pick["price"],  # snapshot at scan time — not the entry
                "target": pick["target"],
                "pick_date": _date.today().isoformat(),
                "score": pick.get("score"),
                "gap_pct": pick.get("gap_pct"),
            }
            try:
                import redis.asyncio as _ra
                import json as _j2
                _redis = _ra.from_url(os.environ.get("REDIS_URL", "redis://edge_redis:6379"), decode_responses=True)
                entry_key = f"theta:pending_entry:{entry_payload['pick_date']}:{user.id}"
                # Only queue if not already queued (idempotent — the daily
                # fire-slot already ensures one pick per day, but in case
                # emit_theta_pick re-fires for any reason we do not want dupes).
                if pick.get("watch_only"):
                    logger.info(
                        f"[stock-entry] SKIP live queue ticker={pick['ticker']} user={user.email} "
                        f"— watch_only/unconfirmed pick (informational only, no live entry)"
                    )
                elif await _redis.set(entry_key, _j2.dumps(entry_payload), ex=36*3600, nx=True):
                    logger.info(
                        f"[stock-entry] QUEUED ticker={pick['ticker']} user={user.email} "
                        f"qty={qty} pick_price=${pick['price']:.2f} — waiting for timing gate"
                    )
                else:
                    logger.info(f"[stock-entry] already queued for {user.email} {pick['ticker']} today — skip")
            except Exception as _qe:
                logger.error(f"[ThetaScanner] failed to enqueue pending entry: {_qe}")
    except Exception as e:
        logger.error(f"[ThetaScanner] entry-queue failed: {e}")

    # Persist to email_signals_history so the Email Signals page can show
    # today's pick + the running 30-day log.
    try:
        from sqlalchemy import text as _t
        # quality_reasons column is added lazily (ADD COLUMN IF NOT EXISTS) so
        # this path is safe even on a DB that predates the migration script.
        await db.execute(_t(
            "ALTER TABLE email_signals_history ADD COLUMN IF NOT EXISTS quality_reasons text"
        ))
        await db.execute(_t(
            "ALTER TABLE email_signals_history ADD COLUMN IF NOT EXISTS chart_b64 text"
        ))
        await db.execute(_t(
            "ALTER TABLE email_signals_history ADD COLUMN IF NOT EXISTS stop_reason text"
        ))
        await db.execute(_t(
            "ALTER TABLE email_signals_history ADD COLUMN IF NOT EXISTS target_reason text"
        ))
        import json as _qj
        # email_signals_history is a GLOBAL pick log, but emit_theta_pick is called
        # once PER subscribed user — guard so we write exactly one history row per
        # (ticker, ET-day). The per-user EMAIL already went out above; this only
        # de-dupes the history/Email-Signals row. Includes stop/target REASON labels.
        _dup = (await db.execute(_t(
            "SELECT 1 FROM email_signals_history WHERE ticker = :tk "
            "AND picked_at::date = (NOW() AT TIME ZONE 'America/New_York')::date LIMIT 1"
        ), {"tk": pick["ticker"]})).first()
        if _dup:
            logger.info(f"[ThetaScanner] history row for {pick['ticker']} already exists today — skip dup insert")
        else:
            await db.execute(_t("""
                INSERT INTO email_signals_history
                  (picked_at, ticker, asset_type, direction, entry, stop, target,
                   gap_pct, rel_vol, today_vol, score, catalyst_reason, quality_reasons,
                   chart_b64, stop_reason, target_reason)
                VALUES (NOW(), :tk, :at, 'long', :en, :st, :tg, :gp, :rv, :tv, :sc, :cr, :qr,
                        :chart, :stop_reason, :target_reason)
            """), {
                "tk": pick["ticker"], "at": pick.get("asset_type", "options"),
                "en": pick["entry"], "st": pick["stop"], "tg": pick["target"],
                "gp": pick["gap_pct"], "rv": pick.get("rel_vol", 0),
                "tv": pick["today_vol"], "sc": pick["score"],
                "cr": pick["catalyst_reason"],
                "qr": _qj.dumps(pick.get("quality_reasons", [])),
                "chart": _chart_b64,
                "stop_reason": stop_reason, "target_reason": target_reason,
            })
            await db.commit()
            logger.info(f"[ThetaScanner] persisted to email_signals_history: {pick['ticker']}")
    except Exception as e:
        logger.error(f"[ThetaScanner] history persist failed: {e}")
    return ok


async def analyze_ticker(db, ticker: str, direction: str = "long") -> dict:
    """On-demand analysis of ANY ticker: live price, structure-based levels
    (compute_levels) and the scanner quality-gate verdict (_apply_quality_filters).
    Read-only — places nothing, emails nothing. NOT a prediction; it reports the
    levels that matter + whether the system sees a tradeable setup right now."""
    from datetime import datetime as _dt, timezone as _tz, date as _date, timedelta as _td
    from app.engines.options.premarket_scheduler import (
        _polygon_1min_bars, _today_et_date_str, _polygon_last_trade_price,
    )
    from app.engines.scanner.levels import compute_levels

    tkr = (ticker or "").upper().strip()
    if not tkr:
        return {"error": "no ticker provided"}
    now = _dt.now(_tz.utc).isoformat()

    price = None
    try:
        price = await _polygon_last_trade_price(tkr)
    except Exception:
        price = None
    try:
        bars = await _polygon_1min_bars(tkr, _today_et_date_str()) or []
    except Exception:
        bars = []
    if (not price or price <= 0) and bars:
        price = float(bars[-1].get("c") or 0)
    if not price or price <= 0:
        return {"ticker": tkr, "error": "no live price available (symbol unknown or no data)",
                "as_of": now}

    prev_close = None
    gap_pct = 0.0
    rel_vol = 0.0          # today vol vs prior-day vol (the scanner's rel-vol metric)
    rel_vol_20d = 0.0      # today vol vs 20-day avg (context)
    today_vol = 0.0
    try:
        from app.api.routes.scanner import _polygon_daily_range
        end = _date.today()
        dr = _polygon_daily_range(tkr, (end - _td(days=45)).isoformat(), end.isoformat()) or []
        if len(dr) >= 2:
            prev_close = float(dr[-2].get("c") or 0)
            if prev_close > 0:
                gap_pct = (price - prev_close) / prev_close * 100.0
            today_vol = float(dr[-1].get("v") or 0)
            prev_vol = float(dr[-2].get("v") or 0)
            if prev_vol > 0:
                rel_vol = round(today_vol / prev_vol, 2)
            vols = [float(b.get("v") or 0) for b in dr[-21:-1]]
            avg = (sum(vols) / len(vols)) if vols else 0.0
            if avg > 0:
                rel_vol_20d = round(today_vol / avg, 2)
    except Exception:
        pass

    cand = {"ticker": tkr, "price": float(price), "gap_pct": round(gap_pct, 2),
            "rel_vol": rel_vol, "catalyst_reason": "on-demand analysis"}
    try:
        verdict, reasons = await _apply_quality_filters(db, cand)
    except Exception as e:
        verdict, reasons = "error", [f"{type(e).__name__}: {e}"]

    lv_long = compute_levels("long", float(price), bars, rr=2.0)
    lv_short = compute_levels("short", float(price), bars, rr=2.0)

    # Coarse momentum match: does it clear gap / rel-vol / price / $-vol for any
    # PROMOTED template? This separates "passes the risk gate" (accept) from
    # "is an actual scanner pick candidate".
    from app.engines.scanner.definitions import enabled_templates
    today_dvol = float(price) * float(today_vol)

    def _coarse_check(tpl):
        df = tpl.daily_filters or {}
        if not (df.get("price_min", 0) <= price <= df.get("price_max", 1e12)):
            return False, f"price ${price:.2f} outside ${df.get('price_min')}-${df.get('price_max')}"
        if not (df.get("gap_min", -1e12) <= gap_pct <= df.get("gap_max", 1e12)):
            return False, f"gap {gap_pct:.1f}% outside {df.get('gap_min')}..{df.get('gap_max')}%"
        if rel_vol < df.get("rel_vol_min", 0):
            return False, f"rel-vol {rel_vol:.2f}x < {df.get('rel_vol_min')}x min"
        if today_dvol < df.get("dollar_vol_min", 0):
            return False, f"$-vol ${today_dvol/1e6:.0f}M < ${df.get('dollar_vol_min')/1e6:.0f}M min"
        return True, None

    tpl_results = []
    for tpl in enabled_templates():
        if tpl.options.eligible:
            continue
        ok_c, why = _coarse_check(tpl)
        tpl_results.append({"template": tpl.key, "passes": ok_c, "fail_reason": why})
    is_candidate = any(t["passes"] for t in tpl_results)
    would_be_pick = is_candidate and verdict == "accept" and lv_long.ok

    # ── Decision label + reason + tags: tell the user what to DO at a glance ──
    vwap = _session_vwap(bars) if bars else None
    dist = ((float(price) - vwap) / vwap * 100.0) if (vwap and vwap > 0) else None
    above_vwap = bool(vwap and float(price) >= vwap)
    hh3 = bool(bars and _last3_higher_highs(bars))
    try:
        _pm = _premarket_dollar_volume(bars) if bars else 0.0
    except Exception:
        _pm = 0.0
    sess_liq = max(float(_pm or 0.0), float(today_dvol))
    overext = bool(dist is not None and dist > 7.0)
    lowvol = (bool(rel_vol and rel_vol < 1.5) and bool(rel_vol_20d and rel_vol_20d < 1.5)) or sess_liq < 1_500_000
    _d = dist if dist is not None else 0.0

    tags = []
    if would_be_pick and above_vwap:
        tags.append("Momentum Confirmed")
    if overext:
        tags += ["Already Extended", "Needs Pullback"]
    if lowvol:
        tags.append("Low Volume")
    if is_candidate and not above_vwap:
        tags.append("Reclaim Needed")
    if is_candidate and above_vwap and not hh3 and not would_be_pick:
        tags.append("Breakout Pending")

    if not bars:
        decision, tone, dreason = "No Trade", "none", "No intraday data to confirm a setup right now."
    elif would_be_pick:
        decision, tone = "Buy", "buy"
        dreason = (f"Momentum confirmed above VWAP (+{_d:.1f}%), rel-vol {rel_vol:.1f}x, "
                   f"{lv_long.rr:.1f}R structure \u2014 clears all filters.")
    elif overext:
        decision, tone = "Avoid", "avoid"
        dreason = f"Already +{_d:.1f}% above VWAP \u2014 overextended; chasing here is poor R:R. Wait for a pullback."
    elif is_candidate and not above_vwap:
        decision, tone = "Wait for Confirmation", "wait"
        dreason = f"Real momentum but {abs(_d):.1f}% below VWAP \u2014 needs to reclaim VWAP before it is a buy."
    elif is_candidate and above_vwap and not lv_long.ok:
        decision, tone = "Do Not Buy", "avoid"
        dreason = f"Setup is there but R:R {lv_long.rr:.1f} is below the minimum \u2014 not worth the risk."
    elif is_candidate and above_vwap:
        decision, tone = "Wait for Confirmation", "wait"
        dreason = "Above VWAP but the breakout is not confirmed yet \u2014 waiting on higher-highs / volume."
    elif sess_liq < 1_000_000:
        decision, tone = "Avoid", "avoid"
        dreason = "Too illiquid to trade safely \u2014 not enough dollar volume."
    elif abs(gap_pct) >= 2.0 or (rel_vol and rel_vol >= 1.5):
        decision, tone = "Watch", "watch"
        dreason = f"Some activity (gap {gap_pct:.1f}%, rel-vol {rel_vol:.1f}x) but below the momentum threshold \u2014 not a setup yet."
    else:
        decision, tone = "No Trade", "none"
        dreason = "No valid setup right now \u2014 no momentum, no confirmation."
    decision_obj = {"label": decision, "tone": tone, "reason": dreason, "tags": tags}

    def _lv(lv):
        return {"entry": lv.entry, "stop": lv.stop, "stop_reason": lv.stop_reason,
                "target": lv.target, "target_reason": lv.target_reason, "rr": lv.rr,
                "projected_move_pct": lv.projected_move_pct, "basis": lv.basis,
                "structurally_valid": lv.ok}

    if would_be_pick:
        summary = f"{tkr}: WOULD be a scanner pick (long) — clears momentum + risk gate, R:R {lv_long.rr}."
    elif is_candidate and verdict != "accept":
        summary = f"{tkr}: momentum candidate but FAILS the risk gate ({'; '.join(str(r) for r in reasons)})."
    elif verdict == "accept" and not is_candidate:
        _fr = next((t["fail_reason"] for t in tpl_results if t["fail_reason"]), "no active momentum")
        summary = f"{tkr}: passes the risk gate but NOT a scanner candidate ({_fr}) — no active setup."
    else:
        summary = f"{tkr}: no setup — neither a momentum candidate nor a gate pass right now."

    return {
        "ticker": tkr,
        "price": round(float(price), 2),
        "price_source": "Polygon/Massive grouped-daily + 1-min bars (Stocks Starter = 15-min delayed)",
        "as_of": now,
        "prev_close": round(prev_close, 2) if prev_close else None,
        "gap_pct": round(gap_pct, 2),
        "rel_vol_vs_prev_day": rel_vol,
        "rel_vol_vs_20d_avg": rel_vol_20d,
        "today_dollar_vol_musd": round(today_dvol / 1e6, 1),
        "gate_long": {"verdict": verdict, "reasons": reasons},
        "scanner_match": {
            "is_candidate": is_candidate,
            "would_be_pick": would_be_pick,
            "templates": tpl_results,
        },
        "levels_long": _lv(lv_long),
        "levels_short": _lv(lv_short),
        "decision": decision_obj,
        "summary": summary,
        "note": ("Structure + gate analysis only — NOT a price prediction or guarantee. "
                 "Gate is long-biased (VWAP); short levels are structural references only."),
    }
