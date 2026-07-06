"""SARO-RANK-UPGRADE finalist enrichment (2026-07-06).

Context: on 7/06 Saro picked GPC off a STALE Polygon snapshot (every number was
Thursday 7/02's session — the delayed tier served a holiday-weekend-old tape)
while STT's Oracle picked IREN (+13% on the day). This module pulls LIVE FMP
data for the top-N funnel finalists ONLY (bounded API cost, ~3-4 requests per
symbol, two of them cached) so re-scoring sees the real session: live price /
live gap, 70 calendar days of daily history (trend / ADV / former-runner
shape) and analyst consensus.

Env-gated by SARO_RANK_UPGRADE at the call site (theta_scanner). Every field
fails SOFT: per-symbol try/except, missing data leaves keys ABSENT so
scoring.score_candidate keeps its neutral 0.5 defaults. Total wall-clock
budget ~20s with 0.15s pacing between symbols.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from loguru import logger

_ET = ZoneInfo("America/New_York")
_BUDGET_S = 20.0
_PACE_S = 0.15


def _bar_date(b: dict):
    """ET calendar date of a Polygon-shaped daily bar. The +12h shift makes the
    result correct whether the source encoded midnight-UTC or midnight-ET."""
    try:
        return datetime.fromtimestamp(float(b["t"]) / 1000.0 + 43200.0,
                                      tz=timezone.utc).date()
    except Exception:
        return None


def _hist_only(bars: list, today_date) -> list:
    """Drop today's (possibly partial) session bar — history must be settled."""
    out = []
    for b in bars or []:
        d = _bar_date(b)
        if d is not None and d < today_date:
            out.append(b)
    return out


def _metrics_from_hist(hist: list, today_vol) -> dict:
    """Pure math over settled daily rows ({t,o,h,l,c,v} ascending). Any field
    it cannot compute is simply left out (scoring stays neutral)."""
    out: dict = {}
    closes = [float(b.get("c") or 0) for b in hist]
    if not closes or closes[-1] <= 0:
        return out
    prev_close = closes[-1]
    out["prev_close_daily"] = prev_close
    if len(closes) >= 6 and closes[-6] > 0:
        out["chg_5d_pct"] = round((prev_close / closes[-6] - 1.0) * 100.0, 2)
    if len(closes) >= 21 and closes[-21] > 0:
        out["chg_20d_pct"] = round((prev_close / closes[-21] - 1.0) * 100.0, 2)
    highs = [float(b.get("h") or 0) for b in hist[-60:] if b.get("h")]
    if highs and max(highs) > 0:
        out["dist_from_60d_high_pct"] = round((prev_close / max(highs) - 1.0) * 100.0, 2)
    last20 = hist[-20:]
    if len(last20) >= 5:
        vols = [float(b.get("v") or 0) for b in last20]
        adv_sh = sum(vols) / len(vols)
        if adv_sh > 0:
            out["adv20_shares"] = round(adv_sh)
            out["adv20_dollars"] = round(
                sum(float(b.get("c") or 0) * float(b.get("v") or 0) for b in last20)
                / len(last20))
            try:
                if today_vol and float(today_vol) > 0:
                    out["rel_vol_adv20"] = round(float(today_vol) / adv_sh, 2)
            except Exception:
                pass
    # former-runner shape: days with |daily ret| >= 10% inside the window
    runs = 0
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and abs(closes[i] / closes[i - 1] - 1.0) >= 0.10:
            runs += 1
    out["former_runner_days"] = runs
    if len(closes) >= 2 and closes[-2] > 0:
        out["prior_day_ret_pct"] = round((closes[-1] / closes[-2] - 1.0) * 100.0, 2)
    try:
        b = hist[-1]
        h, low, c = float(b.get("h") or 0), float(b.get("l") or 0), float(b.get("c") or 0)
        out["prior_day_clv"] = round((c - low) / (h - low), 3) if h > low else 0.5
    except Exception:
        pass
    return out


async def enrich_finalists(cands: list, top_n: int = 12) -> None:
    """Mutate the top_n candidate dicts in place with live-data keys:
    live_price, live_gap_pct, chg_5d_pct, chg_20d_pct, dist_from_60d_high_pct,
    adv20_shares, adv20_dollars, rel_vol_adv20, former_runner_days,
    prior_day_ret_pct, prior_day_clv, analyst_upside_pct, analyst_rating.
    Missing data leaves keys absent. Never raises."""
    from app.engines.data_feeds.fmp_analyst import get_analyst_view
    from app.engines.data_feeds.fmp_feed import (
        fetch_daily_bars_sync,
        fetch_quote_short_price,
    )

    started = time.monotonic()
    today_et = datetime.now(tz=_ET).date()
    start_iso = (today_et - timedelta(days=70)).isoformat()
    end_iso = today_et.isoformat()
    n_ok = 0
    slate = (cands or [])[: max(0, int(top_n))]
    for c in slate:
        if time.monotonic() - started > _BUDGET_S:
            logger.warning(
                f"[saro-enrich] {_BUDGET_S:.0f}s budget exhausted after {n_ok} "
                "symbols — remaining finalists stay un-enriched (neutral)")
            break
        tkr = (c.get("ticker") or "").upper()
        if not tkr:
            continue
        try:
            live = await fetch_quote_short_price(tkr)
            if live and float(live) > 0:
                c["live_price"] = float(live)
            bars = await asyncio.to_thread(
                fetch_daily_bars_sync, tkr, start_iso, end_iso)
            hist = _hist_only(bars, today_et)
            metrics = _metrics_from_hist(hist, c.get("today_vol"))
            c.update(metrics)
            prev_close = metrics.get("prev_close_daily")
            if c.get("live_price") and prev_close:
                c["live_gap_pct"] = round(
                    (float(c["live_price"]) / float(prev_close) - 1.0) * 100.0, 2)
            view = await get_analyst_view(tkr)
            if view and view.get("target"):
                ref = c.get("live_price") or c.get("price")
                if ref and float(ref) > 0:
                    c["analyst_upside_pct"] = round(
                        (float(view["target"]) / float(ref) - 1.0) * 100.0, 2)
                if view.get("rating"):
                    c["analyst_rating"] = str(view["rating"])
            n_ok += 1
        except Exception as e:
            logger.warning(
                f"[saro-enrich] {tkr} failed ({type(e).__name__}: {e}) — "
                "left neutral")
        await asyncio.sleep(_PACE_S)
    logger.info(
        f"[saro-enrich] enriched {n_ok}/{len(slate)} finalists in "
        f"{time.monotonic() - started:.1f}s")
