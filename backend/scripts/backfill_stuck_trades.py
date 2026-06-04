"""Backfill stuck `trades.status='open'` rows when the matching
`open_positions_watch` row is already `status='closed'`.

Run in the backend container:
    docker exec -w /app edge_backend python -m scripts.backfill_stuck_trades
    docker exec -w /app edge_backend python -m scripts.backfill_stuck_trades --dry-run
    docker exec -w /app edge_backend python -m scripts.backfill_stuck_trades --user-email jaceford12@yahoo.com

Why: a bug in _run_trailing_stop_watcher and _check_end_of_day_close (fixed
2026-06-04) updated only the scanner sidecar (open_positions_watch) when a
position exited — never the user-visible `trades` row. So jaceford12's URG
and AIIO trades showed status='open' indefinitely.

The script is idempotent — it only touches rows whose trades.status='open'
AND there's a matching watch row with status='closed' AND closed_at IS NOT
NULL. Re-running it after a clean run is a no-op.

Prints a BEFORE / AFTER table so the operator can verify the sweep without
grepping logs.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger


def _print_table(title: str, rows: list[dict]) -> None:
    print(f"\n=== {title} ({len(rows)} rows) ===")
    if not rows:
        print("(none)")
        return
    keys = ["user_email", "ticker", "status", "entry_price", "exit_price", "exit_reason", "pnl"]
    widths = {k: max(len(k), max((len(str(r.get(k, ""))) for r in rows), default=0)) for k in keys}
    header = " | ".join(k.ljust(widths[k]) for k in keys)
    print(header)
    print("-" * len(header))
    for r in rows:
        print(" | ".join(str(r.get(k, "")).ljust(widths[k]) for k in keys))


async def _fetch_state(db, user_email: str | None) -> list[dict]:
    """Snapshot trades+watch state for every (user, ticker) pair that has
    an open trade and matching closed watch row. Used for BEFORE + AFTER
    table dumps."""
    from sqlalchemy import text as _t
    sql = """
        SELECT
            u.email AS user_email,
            t.instrument AS ticker,
            t.status::text AS status,
            t.entry_price,
            t.exit_price,
            t.exit_reason,
            t.pnl,
            w.exit_price AS watch_exit_price,
            w.exit_reason AS watch_exit_reason,
            w.closed_at AS watch_closed_at
          FROM trades t
          JOIN users u ON u.id = t.user_id
          JOIN open_positions_watch w
            ON w.user_id = t.user_id
           AND UPPER(w.ticker) = UPPER(t.instrument)
           AND w.status = 'closed'
           AND w.closed_at IS NOT NULL
         WHERE t.mode = 'live'
           AND t.status = 'open'
    """
    params: dict = {}
    if user_email:
        sql += " AND u.email = :email"
        params["email"] = user_email
    sql += " ORDER BY u.email, t.instrument"
    rows = (await db.execute(_t(sql), params)).fetchall()
    return [dict(r._mapping) for r in rows]


async def _apply_backfill(db, user_email: str | None) -> int:
    """Apply the UPDATE. Returns the row-count actually touched.

    Uses a CTE to pick the most-recent matching watch row per trade so
    that if a (user, ticker) pair has multiple historical watch rows, we
    pick the latest closed one (matches the live behavior — most recent
    fill wins)."""
    from sqlalchemy import text as _t
    sql = """
        WITH picks AS (
            SELECT DISTINCT ON (t.id)
                t.id          AS trade_id,
                t.user_id,
                t.entry_price,
                t.contracts,
                t.commission,
                w.exit_price  AS w_exit_price,
                w.exit_reason AS w_exit_reason,
                w.closed_at   AS w_closed_at
              FROM trades t
              JOIN users u ON u.id = t.user_id
              JOIN open_positions_watch w
                ON w.user_id = t.user_id
               AND UPPER(w.ticker) = UPPER(t.instrument)
               AND w.status = 'closed'
               AND w.closed_at IS NOT NULL
             WHERE t.mode = 'live'
               AND t.status = 'open'
               -- when user_email is null, the bind below is NULL and the
               -- clause is true for every row (NULL=NULL is unknown, but
               -- :email IS NULL → all)
               AND (:email IS NULL OR u.email = :email)
             ORDER BY t.id, w.closed_at DESC
        )
        UPDATE trades t
           SET status      = 'closed',
               exit_price  = picks.w_exit_price,
               exit_reason = picks.w_exit_reason,
               exit_time   = picks.w_closed_at,
               pnl         = ROUND(((picks.w_exit_price - picks.entry_price) * picks.contracts)::numeric, 2),
               net_pnl     = ROUND(((picks.w_exit_price - picks.entry_price) * picks.contracts - COALESCE(picks.commission, 0))::numeric, 2),
               updated_at  = NOW()
          FROM picks
         WHERE t.id = picks.trade_id
    """
    result = await db.execute(_t(sql), {"email": user_email})
    await db.commit()
    return result.rowcount or 0


async def _main_async(user_email: str | None, dry_run: bool) -> None:
    from app.database import async_session_factory

    async with async_session_factory() as db:
        before = await _fetch_state(db, user_email)
        _print_table("BEFORE — open trades with matching closed watch row", before)

        if not before:
            print("\nNothing to backfill. Exiting clean.")
            return

        if dry_run:
            print("\n--dry-run set: NOT applying UPDATE. Re-run without --dry-run to apply.")
            return

        touched = await _apply_backfill(db, user_email)
        print(f"\nApplied UPDATE: {touched} trades row(s) updated.")

        after = await _fetch_state(db, user_email)
        _print_table("AFTER — should be empty (any remaining = data anomaly)", after)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-email", default=None,
                        help="Scope to one user. Omit to sweep all users.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what WOULD change without writing.")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    try:
        asyncio.run(_main_async(args.user_email, args.dry_run))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
