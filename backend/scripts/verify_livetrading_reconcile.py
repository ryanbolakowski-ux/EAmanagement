#!/usr/bin/env python3
"""Verify Live Trading equity reconciles with starting + realized YTD + open
unrealized for each active broker account.

  docker exec -w /app edge_backend python -m scripts.verify_livetrading_reconcile

For each active broker_account this prints:
  starting_equity (captured or sandbox default)
  realized YTD (SUM(net_pnl COALESCE pnl), live closed trades, since Jan 1)
  open unrealized (placeholder 0 unless wired to a live price source)
  expected equity = start + realized + unrealized
  actual equity   = cached_equity (last broker fetch)
  gap             = actual - expected (>$0.50 is interesting)

Exit code 0 (PASS) always — gaps are EXPECTED for accounts with broker-side
closes not in our trades table; the script's job is to MAKE THEM VISIBLE.
"""
import asyncio
import sys
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import async_session_factory


async def main():
    YTD_START = datetime(datetime.now(timezone.utc).year, 1, 1, tzinfo=timezone.utc)

    async with async_session_factory() as db:
        rows = (await db.execute(text(
            "SELECT u.email, ba.id, ba.broker, ba.account_name, ba.is_demo, "
            "       ba.cached_equity, ba.starting_equity, ba.user_id "
            "  FROM broker_accounts ba JOIN users u ON ba.user_id = u.id "
            " WHERE ba.is_active = true "
            " ORDER BY u.email, ba.account_name"
        ))).all()

        if not rows:
            print("(no active broker accounts)")
            return

        print(f"{'email':<28} {'broker':<10} {'demo':<5} {'start':>10} "
              f"{'rlz_ytd':>10} {'unrlz':>8} {'expected':>11} {'actual':>11} {'gap':>+10}")
        print("-" * 120)
        worst = 0.0
        worst_row = None
        for r in rows:
            try:
                start = float(r.starting_equity) if r.starting_equity is not None else (
                    100_000.0 if (r.is_demo and (r.broker or "").lower() == "tradier")
                    else float(r.cached_equity or 0)
                )
                rlz_row = await db.execute(text(
                    "SELECT COALESCE(SUM(COALESCE(net_pnl, pnl)), 0) FROM trades "
                    " WHERE user_id = :uid AND mode = 'live' AND status = 'closed' "
                    "   AND (exit_time >= :since OR (exit_time IS NULL AND entry_time >= :since))"
                ), {"uid": str(r.user_id), "since": YTD_START})
                rlz = float(rlz_row.scalar() or 0)
                unrlz = 0.0  # no live mark-to-market in this script; matches API when no open trades
                expected = start + rlz + unrlz
                actual = float(r.cached_equity or 0)
                gap = actual - expected
                if abs(gap) > worst:
                    worst = abs(gap)
                    worst_row = (r.email, r.broker, gap)
                print(f"{r.email:<28} {r.broker:<10} {str(r.is_demo):<5} {start:>10.2f} "
                      f"{rlz:>10.2f} {unrlz:>8.2f} {expected:>11.2f} {actual:>11.2f} {gap:>+10.2f}")
            except Exception as e:
                print(f"  [error on {r.email} / {r.broker}]: {e}")

        print("-" * 120)
        print(f"max abs(gap) = ${worst:.2f}" + (f"  ({worst_row[0]} / {worst_row[1]})" if worst_row else ""))
        if worst >= 0.5:
            print("NOTE: a non-zero gap is expected when broker-side closes (e.g. flatten_all)")
            print("      were not written back to the trades table, when commissions/fees are")
            print("      tracked broker-side only, or when recorded entry/exit prices slipped")
            print("      vs actual broker fills. The dashboard now shows this gap explicitly.")


if __name__ == "__main__":
    asyncio.run(main())
