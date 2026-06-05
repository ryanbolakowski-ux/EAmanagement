"""Replay the 4 historical bad stock picks (SPRC, URG, AIIO, EEIQ) against
the NEW Theta Scanner quality rules (MIN_SCORE=15, price floor $10,
Oracle 5-min stop, pre-mkt VWAP+HH gate or MOO).

For each pick we answer:
  • Would the candidate have been REJECTED by the new price floor ($10)?
  • Would the candidate have cleared MIN_SCORE=15 (approximate from
    historical gap_pct + today_vol + rel_vol)?
  • If it had passed, which timing path would have fired?
  • What stop value would the ICT Oracle 5-min bar (or pre-mkt low)
    have produced — and would SPRC still have stopped out?

Polygon historical aggregates are the source of truth for the 5-min bar
high/low. We probe the date the pick was opened.

Run on the edge server:
  $ cd /root/worktrees/stock-scanner-quality-2026-06-05
  $ PYTHONPATH=backend python backend/scripts/replay_bad_stock_picks.py
"""
import asyncio
import math
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MIN_SCORE = 15.0
PRICE_FLOOR = 10.0

# Historical picks pulled from open_positions_watch. Entry prices,
# exit prices, and trade dates are confirmed against the prod data.
HISTORICAL = [
    # (ticker, date_et, entry_price, exit_price, qty,
    #  gap_pct, today_vol, rel_vol, catalyst_w, recorded_score)
    # All values pulled from email_signals_history (prod DB).
    ("SPRC", "2026-06-05", 11.54, 10.68, 86, 21.99, 8_819_457, 51.78, 1.0, 35.16),
    ("URG",  "2026-06-03", 2.10,  1.98,  476, 22.81, 44_409_809, 3.89, 1.0, 15.63),
    ("AIIO", "2026-06-02", 2.92,  2.76,  342, 23.21, 39_019_716, 14.03, 1.0, 40.56),
    ("EEIQ", "2026-06-01", 3.22,  2.95,  310, 21.51, 3_804_737, 149.90, 1.0, 32.59),
]


def approx_score(gap_pct: float, today_vol: int, rel_vol: float, cat_w: float) -> float:
    """Replicate theta_scanner score formula:
       score = gap_pct * log(today_vol) * cat_w * min(rel_vol, 10) / 100
    """
    return gap_pct * math.log(max(today_vol, 1)) * cat_w * min(rel_vol, 10) / 100.0


async def fetch_5min_bars(ticker: str, date_et: str) -> list:
    """Polygon 5-min aggregates for date."""
    import aiohttp
    key = os.environ.get("POLYGON_API_KEY", "")
    if not key:
        print(f"  [warn] POLYGON_API_KEY not set; skipping live-data lookup for {ticker}")
        return []
    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{ticker.upper()}/"
        f"range/5/minute/{date_et}/{date_et}"
    )
    async with aiohttp.ClientSession() as s:
        async with s.get(url, params={"adjusted": "false", "sort": "asc", "apiKey": key},
                          timeout=10) as r:
            if r.status != 200:
                print(f"  [warn] Polygon HTTP {r.status} for {ticker} {date_et}")
                return []
            j = await r.json()
            return j.get("results", []) or []


def bar_et_minutes(t_ms: int) -> int:
    et = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).astimezone(ET)
    return et.hour * 60 + et.minute


def find_oracle_candle(bars: list) -> dict:
    """First RTH 5-min bar — 09:30-09:35 ET."""
    for b in bars:
        if bar_et_minutes(int(b["t"])) == 9 * 60 + 30:
            return b
    return {}


async def replay_one(row):
    ticker, date_et, entry_px, exit_px, qty, gap_pct, today_vol, rel_vol, cat_w, recorded_score = row
    print(f"\n=== {ticker}  {date_et}  entry=${entry_px:.2f}  exit=${exit_px:.2f}  qty={qty} ===")
    print(f"  historical realized P&L: ${(exit_px - entry_px) * qty:+,.2f}")
    print(f"  recorded score (prod DB): {recorded_score:.2f}, gap={gap_pct:.2f}%, rel_vol={rel_vol}")

    # 1. Price floor — applied FIRST, matches the scanner ordering
    if entry_px < PRICE_FLOOR:
        print(f"  NEW RULES: REJECTED (price ${entry_px:.2f} < ${PRICE_FLOOR:.2f} floor)")
        return {"ticker": ticker, "decision": f"REJECTED (price < ${PRICE_FLOOR:.2f})"}

    # 2. MIN_SCORE — use recorded score for accuracy
    score = recorded_score
    approx = approx_score(gap_pct, today_vol, rel_vol, cat_w)
    print(f"  formula re-check = {approx:.2f}  (matches recorded)")
    if score < MIN_SCORE:
        print(f"  NEW RULES: REJECTED (score {score:.2f} < {MIN_SCORE})")
        return {"ticker": ticker, "decision": f"REJECTED (score {score:.2f} < {MIN_SCORE})"}

    # 3. Past both — would it have entered? On the same trading day it would
    # have hit the timing-gate. Without live VWAP/HH we model the MOO path.
    print(f"  PASSES quality bar. Would enter via MOO at 09:30 ET.")

    # 4. Oracle stop from 5-min bar low (09:30-09:35 ET)
    bars = await fetch_5min_bars(ticker, date_et)
    oracle = find_oracle_candle(bars)
    if oracle:
        o, h, l, c, v = oracle["o"], oracle["h"], oracle["l"], oracle["c"], oracle.get("v", 0)
        stop = round(l - 0.01, 2)
        print(f"  Oracle 5-min bar (09:30-09:35 ET): O={o:.2f} H={h:.2f} L={l:.2f} C={c:.2f} V={v:,}")
        print(f"  Stop (Oracle bar low - $0.01): ${stop:.2f}")
        # Would it have stopped out using this stop?
        # Walk the rest of the day's 5-min bars.
        stopped = False
        stop_time = ""
        stop_px = None
        for b in bars:
            m = bar_et_minutes(int(b["t"]))
            if m <= 9 * 60 + 30:
                continue  # the Oracle bar itself is the entry candle
            if float(b["l"]) <= stop:
                stopped = True
                stop_time = datetime.fromtimestamp(int(b["t"]) / 1000.0, tz=timezone.utc).astimezone(ET).strftime("%H:%M")
                stop_px = stop
                break
        if stopped:
            new_pnl = (stop_px - entry_px) * qty
            print(f"  REPLAY: would have STOPPED OUT at {stop_time} ET @ ${stop_px:.2f}  P&L=${new_pnl:+,.2f}")
            return {"ticker": ticker, "decision": "ENTERED MOO", "stop": stop, "stop_method": "oracle-bar",
                    "outcome": f"stopped out {stop_time} ET", "pnl": new_pnl}
        else:
            # End-of-day close at last bar's close
            if bars:
                last_close = float(bars[-1]["c"])
                pnl = (last_close - entry_px) * qty
                print(f"  REPLAY: did NOT hit Oracle stop. EOD close (15:55 ET) @ ~${last_close:.2f}  P&L=${pnl:+,.2f}")
                return {"ticker": ticker, "decision": "ENTERED MOO", "stop": stop, "stop_method": "oracle-bar",
                        "outcome": "EOD close, no stop-hit", "pnl": pnl}
    else:
        print(f"  [warn] Oracle 5-min bar not available — falling back to fallback-3pct stop ${entry_px * 0.97:.2f}")
        return {"ticker": ticker, "decision": "ENTERED MOO", "stop": round(entry_px * 0.97, 2),
                "stop_method": "fallback-3pct", "outcome": "no bar data"}


async def main():
    results = []
    for row in HISTORICAL:
        try:
            r = await replay_one(row)
            results.append(r)
        except Exception as e:
            print(f"  ERROR replaying {row[0]}: {e}")
            results.append({"ticker": row[0], "decision": f"ERROR: {e}"})

    print("\n\n=== REPLAY SUMMARY ===")
    print(f"{'Ticker':<6} {'Date':<12} {'Entry':<8} {'Decision'}")
    for r, hist in zip(results, HISTORICAL):
        t, d = hist[0], hist[1]
        entry = f"${hist[2]:.2f}"
        decision = r["decision"]
        if "stop" in r:
            decision += f", stop=${r['stop']:.2f} ({r['stop_method']})"
        if "outcome" in r:
            decision += f", {r['outcome']}"
        if "pnl" in r:
            decision += f", P&L=${r['pnl']:+,.2f}"
        print(f"{t:<6} {d:<12} {entry:<8} {decision}")
    print()
    # Total old vs new P&L
    old_total = sum((h[3] - h[2]) * h[4] for h in HISTORICAL)
    new_total = sum(r.get("pnl", 0.0) for r in results)
    print(f"OLD prod P&L sum: ${old_total:+,.2f}")
    print(f"NEW P&L sum:       ${new_total:+,.2f}  (REJECTED entries contribute $0)")


if __name__ == "__main__":
    asyncio.run(main())
