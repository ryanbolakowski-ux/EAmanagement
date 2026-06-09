#!/usr/bin/env python3
"""Replay the NEW Theta-Scanner quality filters against the last 7 days of
real stock picks (READ-ONLY).

For every distinct stock pick in the last 7 days — sourced from
`email_signals_history` (the email log) UNION `open_positions_watch`
(source='theta_scanner', the actual entries) — this reconstructs a candidate
dict and runs `_apply_quality_filters` against that pick's OWN trading day of
Polygon 1-min bars, then prints whether the new filters would
ACCEPT / WATCH-ONLY / REJECT it and why.

No writes. Safe to run anytime.

Run inside the container:
  python3 backend/scripts/replay_stock_quality.py
"""
import asyncio
import os
from datetime import datetime, timezone

from sqlalchemy import text

from app.database import async_session_factory
from app.engines.options import theta_scanner as ts


async def _load_picks(db):
    """Return list of pick dicts {date, ticker, gap_pct, rel_vol, score, price}
    deduped by (date, ticker), newest first. email_signals_history carries the
    gap/rel_vol/score; open_positions_watch carries the real entry price."""
    rows = (await db.execute(text("""
        WITH esh AS (
            SELECT DISTINCT ON (picked_at::date, ticker)
                   picked_at::date AS d, ticker,
                   gap_pct, rel_vol, score, entry AS price
              FROM email_signals_history
             WHERE picked_at > NOW() - INTERVAL '7 days'
             ORDER BY picked_at::date, ticker, picked_at DESC
        ),
        opw AS (
            SELECT DISTINCT ON (opened_at::date, ticker)
                   opened_at::date AS d, ticker,
                   NULL::numeric AS gap_pct, NULL::numeric AS rel_vol,
                   NULL::numeric AS score, entry_price AS price
              FROM open_positions_watch
             WHERE source = 'theta_scanner'
               AND opened_at > NOW() - INTERVAL '7 days'
             ORDER BY opened_at::date, ticker, opened_at DESC
        )
        SELECT d, ticker,
               MAX(gap_pct)  AS gap_pct,
               MAX(rel_vol)  AS rel_vol,
               MAX(score)    AS score,
               -- prefer the real entry price from opw, else esh
               COALESCE(MAX(price) FILTER (WHERE src='opw'),
                        MAX(price) FILTER (WHERE src='esh')) AS price
          FROM (
              SELECT *, 'esh' AS src FROM esh
              UNION ALL
              SELECT *, 'opw' AS src FROM opw
          ) u
         GROUP BY d, ticker
         ORDER BY d DESC, ticker
    """))).fetchall()
    picks = []
    for r in rows:
        picks.append({
            "date": r.d,
            "ticker": r.ticker,
            "gap_pct": float(r.gap_pct) if r.gap_pct is not None else 0.0,
            "rel_vol": float(r.rel_vol) if r.rel_vol is not None else 0.0,
            "score": float(r.score) if r.score is not None else 0.0,
            "price": float(r.price) if r.price is not None else 0.0,
        })
    return picks


async def _replay_one(pick: dict) -> tuple:
    """Run the quality gate for one historical pick using THAT day's bars.

    We temporarily monkeypatch premarket_scheduler._today_et_date_str so that
    _apply_quality_filters pulls the pick's OWN date (not today)."""
    import app.engines.options.premarket_scheduler as ps
    date_str = pick["date"].isoformat()
    orig = ps._today_et_date_str
    ps._today_et_date_str = lambda: date_str
    try:
        cand = {
            "ticker": pick["ticker"],
            "price": pick["price"],
            "gap_pct": pick["gap_pct"],
            "rel_vol": pick["rel_vol"],
            "today_vol": 0,
            "score": pick["score"],
            "catalyst_reason": "replay",
        }
        verdict, reasons = await ts._apply_quality_filters(None, cand)
        return verdict, reasons
    finally:
        ps._today_et_date_str = orig


async def main():
    if not os.environ.get("POLYGON_API_KEY"):
        print("WARNING: POLYGON_API_KEY not set — bar filters will be skipped (graceful).")
    async with async_session_factory() as db:
        picks = await _load_picks(db)

    print("=" * 100)
    print("THETA-SCANNER QUALITY-FILTER REPLAY — last 7 days of stock picks (READ-ONLY)")
    print("=" * 100)
    if not picks:
        print("No stock picks found in the last 7 days.")
        return
    header = f"{'DATE':<12}{'TICKER':<8}{'PRICE':>9}{'GAP%':>8}{'RELVOL':>8}{'SCORE':>8}  {'VERDICT':<11} REASON(S)"
    print(header)
    print("-" * 100)
    counts = {"accept": 0, "watch": 0, "reject": 0}
    for p in picks:
        try:
            verdict, reasons = await _replay_one(p)
        except Exception as e:
            verdict, reasons = "error", [f"{type(e).__name__}: {e}"]
        counts[verdict] = counts.get(verdict, 0) + 1
        label = {"accept": "ACCEPT", "watch": "WATCH-ONLY",
                 "reject": "REJECT", "error": "ERROR"}.get(verdict, verdict.upper())
        print(f"{p['date'].isoformat():<12}{p['ticker']:<8}"
              f"{p['price']:>9.2f}{p['gap_pct']:>8.1f}{p['rel_vol']:>8.1f}"
              f"{p['score']:>8.1f}  {label:<11} {', '.join(reasons)}")
    print("-" * 100)
    print(f"TOTAL {len(picks)} picks  |  ACCEPT={counts.get('accept',0)}  "
          f"WATCH-ONLY={counts.get('watch',0)}  REJECT={counts.get('reject',0)}"
          + (f"  ERROR={counts['error']}" if counts.get('error') else ""))
    print("=" * 100)


if __name__ == "__main__":
    asyncio.run(main())
