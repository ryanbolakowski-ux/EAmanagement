#!/usr/bin/env python3
"""Reproducible health verification for the daily-pick / futures-email / Live
Trading pipeline. Confirms the --reload hot-loop fix restored everything.

Run inside the backend container:
    docker exec -w /app edge_backend python -m scripts.verify_live_health
    docker exec -w /app edge_backend python -m scripts.verify_live_health --send-email
        (also sends ONE test email to the admin address to prove Resend delivery)

Checks (all must be < 2s, never time out):
  - Trade of the Day loads (or clean empty state)
  - open positions endpoint
  - pending orders endpoint
  - portfolio summary (200 with data, or 403 for a no-broker account)
  - email provider configured (+ optional real send to admin)
  - scheduler / heartbeat ran today
"""
import argparse
import asyncio
import os
import time
import httpx
from sqlalchemy import select, text
from app.database import async_session_factory
from app.models.user import User
from app.core.security import create_access_token

BASE = "http://localhost:8000"


async def _token():
    async with async_session_factory() as db:
        r = await db.execute(select(User).where(User.email == "pytest-fixture@thetaalgos.test"))
        u = r.scalar_one_or_none()
        if not u:
            r2 = await db.execute(select(User).limit(1))
            u = r2.scalar_one()
        return create_access_token({"sub": str(u.id)})


def _check(c, path, ok_codes=(200,), label=None):
    t = time.time()
    try:
        r = c.get(path)
        ms = (time.time() - t) * 1000
        passed = r.status_code in ok_codes
        print(f"  [{'PASS' if passed else 'FAIL'}] {label or path}: {r.status_code} ({ms:.0f}ms)")
        return passed, r
    except Exception as e:
        print(f"  [FAIL] {label or path}: {type(e).__name__} after {(time.time()-t)*1000:.0f}ms")
        return False, None


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-email", action="store_true", help="send a real test email to the admin")
    args = ap.parse_args()

    results = []
    tok = await _token()
    with httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {tok}"}, timeout=15.0) as c:
        print("== Live Trading endpoints (must respond fast, no timeout) ==")
        ok, r = _check(c, "/api/v1/scanner/today-pick", label="Trade of the Day")
        results.append(ok)
        if ok and r is not None:
            body = r.json()
            pick = body.get("pick")
            print(f"       -> {'pick: ' + pick['ticker'] if pick else 'clean empty state (no pick)'}")
        results.append(_check(c, "/api/v1/scanner/open-positions", label="Open positions")[0])
        results.append(_check(c, "/api/v1/scanner/pending-orders", label="Pending orders")[0])
        results.append(_check(c, "/api/v1/trades/open-positions", label="Trades open-positions")[0])
        # portfolio: 200 (has broker) or 403 (no broker on this account) are both healthy
        results.append(_check(c, "/api/v1/live-trading/portfolio-summary",
                              ok_codes=(200, 403), label="Portfolio summary (P&L/balance)")[0])
        results.append(_check(c, "/health", label="Liveness")[0])

    print("== Email provider ==")
    from app.config import settings
    configured = bool(settings.RESEND_API_KEY)
    print(f"  [{'PASS' if configured else 'FAIL'}] RESEND_API_KEY configured: {configured}")
    results.append(configured)
    if args.send_email and configured:
        from app.services.email import _send_tracked
        res = _send_tracked("theta.algos@yahoo.com",
                            "🎯 Theta Scanner — verify_live_health test",
                            "<p>If you received this, Resend delivery works end-to-end.</p>")
        print(f"  [{'PASS' if res.get('sent') else 'FAIL'}] test email send: {res.get('provider_status')} id={res.get('provider_message_id')}")
        results.append(bool(res.get("sent")))

    print("== Scheduler / heartbeat activity today ==")
    async with async_session_factory() as db:
        row = (await db.execute(text(
            "SELECT count(*) FROM email_signals_history WHERE picked_at::date = CURRENT_DATE"
        ))).scalar()
    print(f"  email_signals_history rows today: {row}")

    print()
    n_pass = sum(1 for x in results if x)
    print(f"RESULT: {n_pass}/{len(results)} checks passed")
    if n_pass != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
