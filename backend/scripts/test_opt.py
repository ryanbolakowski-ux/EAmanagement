import asyncio, uuid, time
from datetime import datetime
from sqlalchemy import text
from app.database import async_session_factory
from app.api.routes.optimization import _run_optimization_task

async def main():
    rid = str(uuid.uuid4())
    sid = "9369ce64-01d2-43fb-b403-db3d00bd3aa7"  # FVG Inversion Tap
    async with async_session_factory() as db:
        uid = (await db.execute(text("SELECT user_id FROM strategies WHERE id=:s"), {"s": sid})).scalar()
        await db.execute(text(
            "INSERT INTO optimization_runs (id, strategy_id, user_id, instrument, start_date, end_date, "
            "parameter_grid, optimization_metric, total_combinations, completed_combinations, status, created_at) "
            "VALUES (:id,:s,:u,'NQ',:sd,:ed, CAST(:grid AS jsonb),'profit_factor',0,0,'QUEUED',NOW())"),
            {"id": rid, "s": sid, "u": uid, "sd": datetime(2026,5,1), "ed": datetime(2026,6,1),
             "grid": '{"stop_loss_ticks":[8,12],"risk_reward_ratio":[2,3]}'})
        await db.commit()
    print(f"created test run {rid[:8]} (4 combos, 1mo NQ)")
    t0=time.time()
    await _run_optimization_task(rid)
    print(f"=== TASK returned after {time.time()-t0:.1f}s ===")
    async with async_session_factory() as db:
        row=(await db.execute(text("SELECT status, total_combinations, completed_combinations, error_message FROM optimization_runs WHERE id=:id"),{"id":rid})).first()
        print("RUN:", dict(row._mapping))
        res=(await db.execute(text("SELECT rank, round(win_rate::numeric,3) wr, round(profit_factor::numeric,2) pf, total_trades, parameters FROM optimization_results WHERE optimization_run_id=:id ORDER BY rank"),{"id":rid})).fetchall()
        for r in res: print("  result:", dict(r._mapping))
        await db.execute(text("DELETE FROM optimization_results WHERE optimization_run_id=:id"),{"id":rid})
        await db.execute(text("DELETE FROM optimization_runs WHERE id=:id"),{"id":rid})
        await db.commit()
    print("cleaned up test run")
if __name__ == "__main__":
    asyncio.run(main())
