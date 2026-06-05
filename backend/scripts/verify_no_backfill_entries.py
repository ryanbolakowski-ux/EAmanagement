"""Detect paper/live trades that were OPENED on a bar predating their session.

Bug (2026-06-05): on session start the data feed replays recent historical
1-min bars as "live"; the strategy entered each as a real trade. jaceford12's
"SMT Divergence Reversal" paper session started 08:40:27 but logged 9 NQ trades
stamped 08:31-08:43 — 6 of them BEFORE the session existed.

This script is DETECTION ONLY (read-only). For every paper/live session it
flags trades whose `entry_time < session.started_at`. Such trades are
backfill artifacts. After the session-start guard ships (paper / options-paper
/ live traders gate entries on `_session_started_at`), NEW sessions can no
longer produce these — but rows already in the DB from before the fix remain
and SHOULD still be flagged here (that proves the detector works).

Run in the backend container:
    docker exec -w /app edge_backend python -m scripts.verify_no_backfill_entries
    docker exec -w /app edge_backend python -m scripts.verify_no_backfill_entries --session <uuid>
    docker exec -w /app edge_backend python -m scripts.verify_no_backfill_entries --grace-seconds 90
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from sqlalchemy import text

from app.database import async_session_factory

# Modes that come from a session-started engine and therefore must never open
# a position on a pre-start bar. (Backtests legitimately replay history.)
_LIVE_MODES = ("paper", "live", "options_paper", "options_live")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default=None, help="Only check this session id")
    ap.add_argument("--grace-seconds", type=int, default=90,
                    help="Allow entries up to this many seconds before started_at "
                         "(matches the engine's mid-formation-bar grace).")
    args = ap.parse_args()

    grace = timedelta(seconds=args.grace_seconds)

    where = "s.mode = ANY(:modes)"
    params: dict = {"modes": list(_LIVE_MODES)}
    if args.session:
        where += " AND s.id = :sid"
        params["sid"] = args.session

    async with async_session_factory() as db:
        # All trades whose entry_time precedes the session start (minus grace).
        rows = (await db.execute(text(f"""
            SELECT s.id AS session_id, s.label, s.mode, s.started_at,
                   u.username, u.email,
                   t.id AS trade_id, t.instrument, t.direction,
                   t.entry_time, t.exit_time, t.exit_reason, t.net_pnl
            FROM trade_sessions s
            JOIN users u ON u.id = s.user_id
            JOIN trades t ON t.session_id = s.id
            WHERE {where}
              AND t.entry_time IS NOT NULL
              AND t.entry_time < (s.started_at - (:grace_s || ' seconds')::interval)
            ORDER BY s.started_at DESC, t.entry_time ASC
        """), {**params, "grace_s": str(args.grace_seconds)})).fetchall()

    if not rows:
        print(f"[verify] NO pre-session-start entries found (grace={args.grace_seconds}s). Clean.")
        return 0

    # Group by session for a readable report.
    by_sess: dict = {}
    for r in rows:
        by_sess.setdefault(r.session_id, []).append(r)

    print(f"[verify] FOUND {len(rows)} backfill entr{'y' if len(rows)==1 else 'ies'} "
          f"across {len(by_sess)} session(s) (entry_time < started_at - {args.grace_seconds}s):\n")
    for sid, trs in by_sess.items():
        head = trs[0]
        delta0 = (head.started_at - head.entry_time)
        print(f"  SESSION {sid} | mode={head.mode} | label={head.label!r} | "
              f"user={head.username or head.email}")
        print(f"    started_at = {head.started_at}  | {len(trs)} pre-start trade(s)")
        for t in trs:
            lead = (t.started_at - t.entry_time).total_seconds()
            print(f"      - {t.instrument:<5} {t.direction:<5} entry={t.entry_time} "
                  f"({lead:+.0f}s before start) exit={t.exit_time} "
                  f"{t.exit_reason or ''} pnl={t.net_pnl if t.net_pnl is not None else 0:.1f}")
        print()

    print("[verify] These are pre-existing rows (created before the session-start "
          "guard shipped) OR a regression if started_at is AFTER this deploy. "
          "Detection working as intended.")
    # Exit non-zero so CI/operators notice, but the rows themselves are expected
    # for jace's historical SMT session.
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
