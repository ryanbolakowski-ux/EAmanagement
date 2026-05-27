#!/usr/bin/env python3
"""Repro / smoke-test for the entry-signal email path.

Runs in two modes:

  1.  Default - sends a fake LONG ES entry alert, measures wall-clock latency
      from detection to send, and prints the structured timing log line.

  2.  --idempotency - fires the SAME signal_id twice and verifies the second
      attempt is suppressed (DUPLICATE path).

Usage (from inside the edge_backend container so prod env vars are present):
    docker exec edge_backend python -m scripts.test_signal_email
    docker exec edge_backend python -m scripts.test_signal_email --idempotency
    docker exec edge_backend python -m scripts.test_signal_email --to other@example.com
"""
import argparse
import os
import sys
import time
import uuid
from datetime import datetime, timezone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default=os.environ.get("TEST_SIGNAL_EMAIL_TO", "ryan.bolakowski@yahoo.com"))
    ap.add_argument("--instrument", default="ES")
    ap.add_argument("--strategy", default="SMOKE-TEST")
    ap.add_argument("--idempotency", action="store_true",
                    help="Fire same signal_id twice and verify dedupe.")
    args = ap.parse_args()

    from app.api.routes.account_signals import send_signal_email

    sid = str(uuid.uuid4())
    entry_detected_at = datetime.now(timezone.utc)
    print(f"[smoke] signal_id={sid}")
    print(f"[smoke] to={args.to}")
    print(f"[smoke] entry_detected_at={entry_detected_at.isoformat()}")

    t0 = time.time()
    ok = send_signal_email(
        to=args.to,
        username="smoke-tester",
        account_label="SMOKE-TEST",
        strategy_name=args.strategy,
        instrument=args.instrument,
        direction="long",
        entry=5000.00, stop=4990.00, target=5020.00,
        bias="bullish",
        fired_at=entry_detected_at.strftime("%a, %b %-d %-I:%M %p ET"),
        signal_id=sid,
        entry_detected_at=entry_detected_at,
    )
    elapsed_ms = int((time.time() - t0) * 1000)
    print(f"[smoke] first attempt: ok={ok} elapsed_ms={elapsed_ms}")
    if not ok:
        print("[smoke] FIRST ATTEMPT WAS SUPPRESSED - check the log line above")
        print("[smoke] common reasons: DEAD zone, session cap, killswitch, Redis down")

    if args.idempotency:
        time.sleep(1)
        t1 = time.time()
        ok2 = send_signal_email(
            to=args.to, username="smoke-tester", account_label="SMOKE-TEST",
            strategy_name=args.strategy, instrument=args.instrument, direction="long",
            entry=5000.00, stop=4990.00, target=5020.00, bias="bullish",
            fired_at=entry_detected_at.strftime("%a, %b %-d %-I:%M %p ET"),
            signal_id=sid,
            entry_detected_at=entry_detected_at,
        )
        print(f"[smoke] second attempt (same signal_id): ok={ok2} elapsed_ms={int((time.time()-t1)*1000)}")
        if ok2:
            print("[smoke] FAIL - idempotency key did not block the duplicate")
            sys.exit(2)
        print("[smoke] PASS - duplicate was suppressed")


if __name__ == "__main__":
    main()
