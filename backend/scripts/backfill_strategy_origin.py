"""Backfill strategies.origin — STRATEGY-VISIBILITY-V1.

Marks platform-seeded strategy rows origin='seeded' so the list surfaces
(GET /api/v1/strategies/ and GET /api/v1/backtests/ranking) hide them,
leaving each user with only the strategies they created themselves
(Ryan's rule: "every new user only sees their strategys they clicked
create in the database and added to their list").

VISIBILITY ONLY — this script never touches status / require_confirm /
updated_at (raw SQL UPDATE of the single origin column, so the ORM
onupdate on updated_at is bypassed), never deletes anything, and by-id
access is deliberately untouched: account_signal_watchers keep emailing,
paper/live trade_sessions keep running, the auto-execute scan loop keeps
selecting by status, and completed backtest detail pages keep resolving.

HEURISTIC (eyeballed against all 242 prod rows on 2026-08-07 — zero
misclassifications):

    seeded := (name IN CANONICAL_24) AND (same-second batch size >= 3)

  * CANONICAL_24 = the 17 signup-seed names (imported from
    app.scripts.seed_strategies.SEED — single source of truth) + the 7
    one-shot scanner backfill names below.
  * batch size = number of strategy rows TABLE-WIDE sharing this row's
    date_trunc('second', created_at). Every seed path inserted
    same-second batches (signup seeds: 17 rows within ~200 microseconds;
    cross-user scanner backfills: identical microsecond timestamps
    across >= 3 users). Every organic create is a solitary insert.
  * Rejected alternatives (each disproven on prod rows): name-only
    (bcf150's organic ACTIVE "Judas Swing" and jaceford's organic DRAFT
    "Reversal Swing" share canonical names), created_at ~= user signup
    (May users were backfilled weeks after signup), empty rule_tree
    (jaceford's organic "Rejection block" has rule_tree='{}').
  * Failure polarity errs toward VISIBLE: a renamed seeded row stays
    visible; a solitary organic create can never be hidden.

COLUMN POLARITY: origin is nullable; NULL is treated as user-created
everywhere (filters use origin IS DISTINCT FROM 'seeded'), so any row
written by an unpatched create path stays visible and post-backfill
user-created rows need no write. API create paths set origin='user'
explicitly (belt + suspenders); seed_user_strategies sets
origin='seeded' on insert.

Idempotent + rerun-safe: ADD COLUMN IF NOT EXISTS, then UPDATE only rows
still NULL — a second run reports 0 newly hidden. ALTER + UPDATE happen
in one transaction (Postgres DDL is transactional).

Run in the backend container (the orchestrator runs this at deploy):

    docker exec -w /app edge_backend python -m scripts.backfill_strategy_origin --dry-run
    docker exec -w /app edge_backend python -m scripts.backfill_strategy_origin

--dry-run issues ONLY SELECTs (no DDL, no UPDATE) and prints the same
per-user projection so the operator can eyeball who loses what before
committing.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from datetime import datetime

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from sqlalchemy import bindparam, text  # noqa: E402

# The 17 canonical signup-seed names come straight from the seed library so
# the two can never drift apart.
from app.scripts.seed_strategies import SEED  # noqa: E402

#: The 7 scanner strategies written by one-time cross-user backfills on
#: 2026-05-13 01:38/01:45/02:22 and 2026-05-20 03:12 (identical microsecond
#: created_at across >= 3 users). No live code recreates them (grep-verified;
#: Aug signups got none) — a backfill-only concern.
SCANNER_SEEDED_NAMES: tuple[str, ...] = (
    "Momentum Gappers",
    "Low-Float Squeeze",
    "52-Week High Breakout",
    "Pre-Market Gap Runner",
    "Oracle — 5-Minute Opening Candle",
    "Futures Signal Scanner (ICT)",
    "Theta Scanner",
)

CANONICAL_SEEDED_NAMES: frozenset[str] = (
    frozenset(t["name"] for t in SEED) | frozenset(SCANNER_SEEDED_NAMES)
)

#: Minimum table-wide same-second batch size for a canonical-named row to
#: classify as seeded. Every observed seed batch is >= 3 (signup seeds are
#: 17-row batches); every observed organic create is a solitary insert (1).
MIN_BATCH = 3


def classify_seeded(rows: list[tuple[str, datetime]]) -> list[bool]:
    """Pure-python reference of the SQL heuristic (unit-tested; must stay in
    lockstep with the SQL below). `rows` is the ENTIRE strategies table as
    (name, created_at) tuples; returns one bool per row — True = seeded.
    datetime.replace(microsecond=0) is equivalent to Postgres
    date_trunc('second', ...)."""
    batch = Counter(dt.replace(microsecond=0) for _, dt in rows)
    return [
        (name in CANONICAL_SEEDED_NAMES) and batch[dt.replace(microsecond=0)] >= MIN_BATCH
        for name, dt in rows
    ]


def _names_stmt(sql: str):
    """text() with the :names list bind expanded (asyncpg-safe IN)."""
    return text(sql).bindparams(bindparam("names", expanding=True))


# {hidden_now} / {null_guard} are swapped depending on whether the origin
# column exists yet, so --dry-run works on today's prod schema without DDL.
_SUMMARY_SQL_TMPL = """
    WITH batches AS (
        SELECT date_trunc('second', created_at) AS sec, COUNT(*) AS n
          FROM strategies
         GROUP BY 1
    ),
    cls AS (
        SELECT s.user_id,
               (CASE WHEN {hidden_now} THEN 1 ELSE 0 END) AS hidden_now,
               (CASE WHEN s.name IN :names AND b.n >= {min_batch} {null_guard}
                     THEN 1 ELSE 0 END) AS matched
          FROM strategies s
          JOIN batches b ON b.sec = date_trunc('second', s.created_at)
    )
    SELECT u.email                        AS email,
           COUNT(c.hidden_now)            AS total,
           COALESCE(SUM(c.hidden_now), 0) AS hidden_before,
           COALESCE(SUM(c.matched), 0)    AS newly_hidden
      FROM users u
      LEFT JOIN cls c ON c.user_id = u.id
     GROUP BY u.email
     ORDER BY u.email
"""


async def _column_exists(db) -> bool:
    r = await db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'strategies' AND column_name = 'origin'"
    ))
    return r.first() is not None


def _print_summary(rows, title: str) -> None:
    print(f"\n=== per-user summary — {title} ===")
    hdr = (f"{'email':<40} {'total':>5} {'hidden_before':>13} "
           f"{'newly_hidden':>12} {'hidden_after':>12} {'visible_after':>13}")
    print(hdr)
    print("-" * len(hdr))
    tt = th = tn = 0
    for r in rows:
        total, before, newly = int(r.total), int(r.hidden_before), int(r.newly_hidden)
        after = before + newly
        print(f"{(r.email or '?'):<40} {total:>5} {before:>13} "
              f"{newly:>12} {after:>12} {total - after:>13}")
        tt += total
        th += before
        tn += newly
    print("-" * len(hdr))
    print(f"{'TOTAL':<40} {tt:>5} {th:>13} {tn:>12} {th + tn:>12} {tt - (th + tn):>13}")


async def run(dry_run: bool) -> int:
    from app.database import async_session_factory, engine

    names = sorted(CANONICAL_SEEDED_NAMES)
    try:
        async with async_session_factory() as db:
            col = await _column_exists(db)
            print(f"strategies.origin column exists: {col}")

            if not dry_run and not col:
                await db.execute(text(
                    "ALTER TABLE strategies ADD COLUMN IF NOT EXISTS origin VARCHAR(16)"
                ))
                col = True
                print("added strategies.origin (VARCHAR(16), NULL)")

            null_guard = "AND s.origin IS NULL" if col else ""
            hidden_now = "s.origin = 'seeded'" if col else "FALSE"
            summary_sql = _SUMMARY_SQL_TMPL.format(
                hidden_now=hidden_now, null_guard=null_guard, min_batch=MIN_BATCH,
            )
            before = (await db.execute(_names_stmt(summary_sql), {"names": names})).fetchall()
            _print_summary(before, "dry-run projection" if dry_run else "BEFORE")

            if dry_run:
                print("\n[dry-run] no changes written (SELECT-only — no DDL, no UPDATE).")
                return 0

            upd = await db.execute(_names_stmt(f"""
                UPDATE strategies AS s
                   SET origin = 'seeded'
                 WHERE s.origin IS NULL
                   AND s.name IN :names
                   AND (SELECT COUNT(*) FROM strategies b
                         WHERE date_trunc('second', b.created_at)
                             = date_trunc('second', s.created_at)) >= {MIN_BATCH}
            """), {"names": names})
            print(f"\nUPDATE: {upd.rowcount} rows -> origin='seeded' (status/updated_at untouched)")
            await db.commit()

            after_sql = _SUMMARY_SQL_TMPL.format(
                hidden_now="s.origin = 'seeded'",
                null_guard="AND s.origin IS NULL",
                min_batch=MIN_BATCH,
            )
            after = (await db.execute(_names_stmt(after_sql), {"names": names})).fetchall()
            _print_summary(after, "AFTER (rerun would newly hide 0)")
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mark platform-seeded strategy rows origin='seeded' (visibility only).",
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="SELECT-only projection; no DDL, no UPDATE")
    args = ap.parse_args()
    return asyncio.run(run(dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
