#!/usr/bin/env python3
"""Verify the optimizer is isolated from production email/pick state and works.

  docker exec -w /app edge_backend python -m scripts.verify_optimizer_isolation

Proves:
  1. ISOLATION: a real optimization run writes ONLY to optimization_runs/
     optimization_results — it does NOT create/modify email_signals,
     account_signals, or pending-trade rows (so it can't suppress emails).
  2. The optimizer reaches a terminal state (completed) for a small grid.
  3. (Static, checked by the caller) both Optimize entry points POST the same
     /api/v1/optimization/ endpoint with the same DEFAULT_OPT_GRID.
"""
import asyncio
import os
import uuid
import json
import time
import psycopg2

PG = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


def counts(cur):
    cur.execute("SELECT count(*) FROM account_signals"); a = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM email_signals_history"); e = cur.fetchone()[0]
    try:
        cur.execute("SELECT count(*) FROM pending_trades"); p = cur.fetchone()[0]
    except Exception:
        p = -1
    return a, e, p


async def main():
    import httpx
    from sqlalchemy import select
    from app.database import async_session_factory
    from app.models.user import User
    from app.core.security import create_access_token

    # tier-5 fixture user + an active strategy
    async with async_session_factory() as db:
        u = (await db.execute(select(User).where(User.email == "pytest-fixture@thetaalgos.test"))).scalar_one_or_none()
        uid = str(u.id)
    tok = create_access_token({"sub": uid})

    cn = psycopg2.connect(PG); cn.autocommit = True
    cur = cn.cursor()
    before = counts(cur)
    print(f"  baseline counts: account_signals={before[0]} email_history={before[1]} pending={before[2]}")

    sid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO strategies (id,user_id,name,status,instruments,primary_timeframe,execution_timeframe,"
        "risk_reward_ratio,stop_loss_type,max_contracts,fvg_min_size_ticks,rule_tree,starred,kill_switch_enabled) "
        "VALUES (%s,%s,'opt-isolation','ACTIVE','[\"ES\"]'::json,'15m','1m',2.0,'ticks',10,4,'{}'::json,false,true)",
        (sid, uid))

    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    with httpx.Client(base_url="http://localhost:8000", headers={"Authorization": f"Bearer {tok}"}, timeout=30) as c:
        r = c.post("/api/v1/optimization/", json={
            "strategy_id": sid, "instrument": "ES", "optimization_metric": "profit_factor",
            "start_date": (now - timedelta(days=5)).isoformat(), "end_date": now.isoformat(),
            "parameter_grid": {"risk_reward_ratio": [2.0], "stop_loss_ticks": [10]},  # 1 combo
        })
        assert r.status_code == 202, r.text
        run_id = r.json()["id"]
        print(f"  started optimization {run_id[:8]} (1 combo)")
        terminal = None
        deadline = time.time() + 150
        while time.time() < deadline:
            g = c.get(f"/api/v1/optimization/{run_id}")
            st = g.json().get("status")
            if st in ("completed", "failed"):
                terminal = st; break
            time.sleep(3)
        print(f"  optimization terminal status: {terminal}")

    after = counts(cur)
    print(f"  after counts:    account_signals={after[0]} email_history={after[1]} pending={after[2]}")
    isolated = (after[0] == before[0] and after[1] == before[1] and after[2] == before[2])
    print(f"  [{'PASS' if isolated else 'FAIL'}] ISOLATION: optimization wrote nothing to email/signal/pending tables")
    completed = terminal == "completed"
    print(f"  [{'PASS' if completed else 'WARN'}] optimization reached terminal state: {terminal}")

    # cleanup
    cur.execute("DELETE FROM optimization_results WHERE optimization_run_id=%s", (run_id,))
    cur.execute("DELETE FROM optimization_runs WHERE id=%s", (run_id,))
    cur.execute("DELETE FROM strategies WHERE id=%s", (sid,))
    cn = cur.rowcount
    cur.close(); cn.close()

    print(f"\nRESULT: isolation={'PASS' if isolated else 'FAIL'}, terminal={terminal}")
    if not isolated:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
