"""Force-sync KYC status from Stripe for one user or every pending user.

Run inside the backend container:
    docker exec -w /app edge_backend python -m scripts.force_sync_kyc --all-pending
    docker exec -w /app edge_backend python -m scripts.force_sync_kyc --user-email foo@bar.com

Prints a table showing every user touched + before/after status, so an admin
can verify the sweep without grepping logs.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from loguru import logger


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no users matched)")
        return
    headers = ["email", "session_id", "before", "after"]
    widths = {h: max(len(h), max((len(str(r.get(h) or "")) for r in rows), default=0)) for h in headers}
    line = " | ".join(h.ljust(widths[h]) for h in headers)
    print(line)
    print("-" * len(line))
    for r in rows:
        print(" | ".join(str(r.get(h) or "").ljust(widths[h]) for h in headers))
    transitions = sum(1 for r in rows if r.get("before") != r.get("after") and r.get("after"))
    print(f"\nswept={len(rows)} transitioned={transitions}")


async def _sweep_all_pending() -> list[dict]:
    from sqlalchemy import text as _t
    from app.database import async_session_factory
    from app.api.routes.kyc import sync_kyc_status_from_stripe
    async with async_session_factory() as db:
        rows = (await db.execute(_t(
            "SELECT id::text AS id, email, kyc_status, kyc_session_id "
            "FROM users "
            "WHERE kyc_status = 'pending' AND kyc_session_id IS NOT NULL"
        ))).mappings().all()
    out: list[dict] = []
    for r in rows:
        before = r["kyc_status"]
        sid = r["kyc_session_id"]
        after = None
        try:
            async with async_session_factory() as db2:
                after = await sync_kyc_status_from_stripe(
                    db2, user_id=str(r["id"]), session_id=sid
                )
        except Exception as e:
            logger.error(f"[force-sync] user={r['email']} failed: {e}")
        out.append({"email": r["email"], "session_id": sid,
                    "before": before, "after": after or before})
    return out


async def _sweep_one_email(email: str) -> list[dict]:
    from sqlalchemy import text as _t
    from app.database import async_session_factory
    from app.api.routes.kyc import sync_kyc_status_from_stripe
    async with async_session_factory() as db:
        rows = (await db.execute(_t(
            "SELECT id::text AS id, email, kyc_status, kyc_session_id "
            "FROM users WHERE email = :em"
        ), {"em": email})).mappings().all()
    if not rows:
        print(f"(no user found with email={email!r})", file=sys.stderr)
        return []
    out: list[dict] = []
    for r in rows:
        sid = r["kyc_session_id"]
        before = r["kyc_status"]
        after = None
        if not sid:
            print(f"(user {r['email']} has no kyc_session_id; nothing to sync)",
                  file=sys.stderr)
        else:
            try:
                async with async_session_factory() as db2:
                    after = await sync_kyc_status_from_stripe(
                        db2, user_id=str(r["id"]), session_id=sid
                    )
            except Exception as e:
                logger.error(f"[force-sync] user={r['email']} failed: {e}")
        out.append({"email": r["email"], "session_id": sid or "",
                    "before": before, "after": after or before})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Force-sync KYC status from Stripe Identity."
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--all-pending", action="store_true",
                   help="Sweep every user with kyc_status='pending' AND a session id.")
    g.add_argument("--user-email", type=str,
                   help="Force-sync exactly one user by email.")
    args = parser.parse_args()

    loop = asyncio.new_event_loop()
    try:
        if args.all_pending:
            rows = loop.run_until_complete(_sweep_all_pending())
        else:
            rows = loop.run_until_complete(_sweep_one_email(args.user_email))
    finally:
        loop.close()

    _print_table(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
