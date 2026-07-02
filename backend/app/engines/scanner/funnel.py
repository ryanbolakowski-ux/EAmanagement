"""Multi-strategy scan funnel (SCANNER-V1, P2).

  Stage 0  universe   — grouped-daily snapshot (~12k tickers), reuse momentum_scanner
  Stage 1  coarse     — template.daily_filters + price/liquidity, ZERO network
  Stage 2  score+rank — canonical 0-100, take top-N (logs every stage)
  Stage 3  confirm    — top-N only: 1-min bars -> structure levels (compute_levels)
  Stage 4  gate       — verdict + rich payload (TemplateHit)

Runs in SHADOW: returns ranked TemplateHits, emits/persists/trades NOTHING. A
template's hits only reach the live emit/trade path after it is promoted
(enabled=True) following real validation — a separate, deliberate step.
"""
from __future__ import annotations

from loguru import logger

from app.engines.scanner.scoring import score_candidate
from app.engines.scanner.levels import compute_levels
from app.engines.scanner.templates import TemplateHit


# Leveraged / inverse ETFs (2x/3x, -1x/-2x/-3x, vol products) — these gap because
# their UNDERLYING moved, not on an equity catalyst; they decay and are not real
# stock breakouts. Excluded from the stock scanner universe.
_LEVERAGED_ETFS = frozenset({
    "SOXL","SOXS","TQQQ","SQQQ","SPXL","SPXS","UPRO","SPXU","TNA","TZA","UDOW","SDOW",
    "QLD","QID","SSO","SDS","DDM","DXD","LABU","LABD","NAIL","DRN","DRV","YINN","YANG",
    "NUGT","DUST","JNUG","JDST","GUSH","DRIP","ERX","ERY","FAS","FAZ","BOIL","KOLD",
    "UCO","SCO","AGQ","ZSL","UGL","GLL","UVXY","VXX","SVXY","UVIX","SVIX","VIXY",
    "TMF","TMV","TSLL","TSLQ","TSLS","NVDL","NVDU","NVDS","NVD","CONL","COND","BITX",
    "BITI","ETHU","MSTU","MSTZ","MSTX","FNGU","FNGD","BULZ","WEBL","WEBS","HIBL","HIBS",
    # 2026-07-02: single-name 2x ETFs that slipped the list — V1 picked METU (2x META)
    # as its daily stock pick on 7/2; FBL was #4 the same day. These gap on the
    # underlying, not an equity catalyst — same rationale as NVDL/TSLL above.
    "METU","METD","FBL","FBS","AAPU","AAPD","AMZU","AMZD","MSFU","MSFD","GGLL","GGLS","NFLU","NFLD",
    "DPST","WANT","CWEB","KORU","INDL","MEXX","BRZU","EDC","EDZ","TYD","TYO","URTY","SRTY",
    "TWM","UWM","SAA","SDD","MVV","MZZ","QID","RXL","CURE","UYG","SKF","SRS","DRN",
})


def _coarse(tpl, row):
    """Apply the template's daily_filters to a grouped-daily snapshot row.
    Returns a candidate dict or None. Pure math, no network."""
    df = tpl.daily_filters or {}
    try:
        price = float(row.get("day", {}).get("c") or 0)
        prev = float(row.get("prevDay", {}).get("c") or 0)
        vol = float(row.get("day", {}).get("v") or 0)
        pvol = float(row.get("prevDay", {}).get("v") or 0)
    except Exception:
        return None
    tkr = (row.get("ticker") or "").upper()
    if price <= 0 or prev <= 0 or not tkr:
        return None
    if "." in tkr or "/" in tkr or tkr.endswith("W") or tkr.endswith("WS"):
        return None
    if tkr in _LEVERAGED_ETFS:
        return None
    gap = (price - prev) / prev * 100.0
    relvol = (vol / pvol) if pvol > 0 else 0.0
    dvol = price * vol
    if not (df.get("price_min", 0) <= price <= df.get("price_max", 1e12)):
        return None
    if not (df.get("gap_min", -1e12) <= gap <= df.get("gap_max", 1e12)):
        return None
    if relvol < df.get("rel_vol_min", 0):
        return None
    if dvol < df.get("dollar_vol_min", 0):
        return None
    return {"ticker": tkr, "price": price, "gap_pct": round(gap, 2),
            "rel_vol": round(relvol, 2), "today_vol": vol, "dollar_vol": dvol}


async def run_funnel(tpl, db, *, top_n: int = 8, confirm: bool = True) -> dict:
    """Run one template through the funnel. SHADOW only."""
    from app.engines.options.momentum_scanner import _fetch_market_snapshot
    from app.engines.options.theta_scanner import _get_8k_catalyst
    from app.engines.options.premarket_scheduler import _polygon_1min_bars, _today_et_date_str

    rows = await _fetch_market_snapshot() or []
    cands = [c for c in (_coarse(tpl, r) for r in rows) if c]
    logger.info(f"[scanner] {tpl.key} stage0 universe={len(rows)} | stage1 passed={len(cands)}")

    for c in cands:
        try:
            cw, creason = await _get_8k_catalyst(db, c["ticker"])
        except Exception:
            cw, creason = 1.0, None
        sb = score_candidate({**c, "catalyst_weight": cw, "catalyst_reason": creason},
                             atr_min_pct=tpl.atr_min_pct, atr_max_pct=tpl.atr_max_pct)
        c["score"] = sb.total
        c["why"] = sb.why()
        c["catalyst_reason"] = creason

    cands.sort(key=lambda x: x["score"], reverse=True)
    top = [c for c in cands if c["score"] >= tpl.min_score_consider][:top_n]
    logger.info(f"[scanner] {tpl.key} stage2 top={len(top)} (>= MIN_SCORE {tpl.min_score_consider})")

    hits = []
    date_et = _today_et_date_str()
    for c in top:
        bars = None
        if confirm:
            try:
                bars = await _polygon_1min_bars(c["ticker"], date_et)
            except Exception as e:
                logger.warning(f"[scanner] {tpl.key} confirm {c['ticker']} bars failed: {e}")
        lv = compute_levels("long", c["price"], bars, rr=tpl.levels.rr_ratio,
                            atr_stop_mult=tpl.levels.atr_stop_mult)
        would_confirm = lv.ok and c["score"] >= tpl.min_score_confirm
        hits.append(TemplateHit(
            ticker=c["ticker"], direction="long", price=c["price"], entry=lv.entry,
            stop=lv.stop, target=lv.target, score=c["score"], strategy_key=tpl.key,
            instrument_type="watch_only", watch_only=True,  # shadow: never tradeable yet
            stop_reason=lv.stop_reason, target_reason=lv.target_reason, rr=lv.rr,
            projected_move_pct=lv.projected_move_pct, why_selected=c["why"],
            reason=(f"would CONFIRM (score>={tpl.min_score_confirm:.0f}, R:R ok)"
                    if would_confirm else "watch-only"),
            invalidation=f"loses {lv.stop_reason}",
            metadata={"levels_basis": lv.basis, "gap_pct": c["gap_pct"],
                      "rel_vol": c["rel_vol"], "would_confirm": would_confirm},
        ))
        logger.info(f"[scanner] {tpl.key} stage3 {c['ticker']} score={c['score']} "
                    f"stop={lv.stop} ({lv.stop_reason}) target={lv.target} rr={lv.rr} "
                    f"basis={lv.basis} would_confirm={would_confirm}")

    return {"template": tpl.key, "universe": len(rows), "passed": len(cands),
            "top_n": len(top), "hits": hits}
