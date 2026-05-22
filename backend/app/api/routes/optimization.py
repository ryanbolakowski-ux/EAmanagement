from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel
from datetime import datetime
from typing import Optional

from app.database import get_db
from app.models.user import User, SubscriptionTier
from app.models.strategy import Strategy
from app.models.optimization import OptimizationRun, OptimizationStatus, OptimizationResult
from app.core.auth import get_current_user, require_tier

router = APIRouter()

live_tiers = [SubscriptionTier.TIER_3, SubscriptionTier.TIER_4, SubscriptionTier.TIER_5]


class OptimizationRequest(BaseModel):
    strategy_id: str
    instrument: str = "ES"
    start_date: datetime
    end_date: datetime
    parameter_grid: dict
    optimization_metric: str = "profit_factor"


class OptimizationRunResponse(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    instrument: str
    status: str
    total_combinations: int
    completed_combinations: int
    created_at: str


class OptimizationResultResponse(BaseModel):
    rank: int
    parameters: dict
    net_profit: float
    profit_factor: float
    win_rate: float
    max_drawdown: float
    total_trades: int
    sharpe_ratio: Optional[float]





# ─── Cross-user optimization result cache ────────────────────────────────
import hashlib as _hash_lib
import json as _json_lib

def _opt_cache_key(strategy_name: str, param_grid: dict, instrument: str, start: str, end: str) -> str:
    """Stable hash so different users with same params hit the same cache entry."""
    payload = {
        "strategy": strategy_name,
        "grid": _json_lib.dumps(param_grid, sort_keys=True),
        "instrument": instrument,
        "start": str(start), "end": str(end),
    }
    raw = _json_lib.dumps(payload, sort_keys=True)
    return _hash_lib.sha256(raw.encode()).hexdigest()[:32]


async def _try_cached_optimization(db, cache_key: str):
    """Return cached results dict if hit, else None."""
    from sqlalchemy import text as _t
    r = (await db.execute(_t("""
        SELECT results FROM optimization_cache WHERE cache_key = :k
    """), {"k": cache_key})).first()
    if not r: return None
    # Bump usage stats
    await db.execute(_t("""
        UPDATE optimization_cache SET run_count = run_count + 1, last_used_at = NOW()
         WHERE cache_key = :k
    """), {"k": cache_key})
    await db.commit()
    return r.results  # JSONB → already a Python list


async def _store_optimization_cache(db, cache_key: str, strategy_name: str,
                                     param_grid: dict, instrument: str,
                                     start, end, results: list, user_id):
    from sqlalchemy import text as _t
    try:
        await db.execute(_t("""
            INSERT INTO optimization_cache
                (cache_key, strategy_signature, parameter_grid, instrument,
                 start_date, end_date, results, first_user_id)
            VALUES (:k, :sn, :pg::jsonb, :inst, :sd, :ed, :r::jsonb, :uid)
            ON CONFLICT (cache_key) DO NOTHING
        """), {
            "k": cache_key, "sn": strategy_name,
            "pg": _json_lib.dumps(param_grid), "inst": instrument,
            "sd": start, "ed": end,
            "r": _json_lib.dumps(results), "uid": str(user_id) if user_id else None,
        })
        await db.commit()
    except Exception as _e:
        from loguru import logger as _l; _l.warning(f"opt-cache store failed: {_e}")
# ─── end cache helpers ──────────────────────────────────────────────────

@router.get("/", response_model=list[OptimizationRunResponse])
async def list_optimizations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OptimizationRun, Strategy.name)
        .join(Strategy, OptimizationRun.strategy_id == Strategy.id)
        .where(OptimizationRun.user_id == current_user.id)
        .order_by(OptimizationRun.created_at.desc())
        .limit(20)
    )
    return [
        OptimizationRunResponse(
            id=str(r.id), strategy_id=str(r.strategy_id),
            strategy_name=strategy_name,
            instrument=r.instrument,
            status=r.status.value, total_combinations=r.total_combinations,
            completed_combinations=r.completed_combinations, created_at=r.created_at.isoformat(),
        )
        for r, strategy_name in result.all()
    ]


@router.post("/", response_model=OptimizationRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def start_optimization(
    data: OptimizationRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_tier(*live_tiers)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # Validate date range - max 3 years
    from datetime import timedelta
    max_range = timedelta(days=365 * 3)
    if data.end_date - data.start_date > max_range:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 3 years.")

    from itertools import product
    combos = list(product(*data.parameter_grid.values()))
    total  = len(combos)

    run = OptimizationRun(
        strategy_id=strategy.id,
        user_id=current_user.id,
        instrument=data.instrument,
        start_date=data.start_date,
        end_date=data.end_date,
        parameter_grid=data.parameter_grid,
        optimization_metric=data.optimization_metric,
        total_combinations=total,
        status=OptimizationStatus.QUEUED,
    )
    db.add(run)
    await db.commit()

    import asyncio
    background_tasks.add_task(_run_optimization_wrapper, str(run.id))

    return OptimizationRunResponse(
        id=str(run.id), strategy_id=str(run.strategy_id),
        strategy_name=strategy.name,
        instrument=run.instrument,
        status=run.status.value, total_combinations=run.total_combinations,
        completed_combinations=run.completed_combinations, created_at=run.created_at.isoformat(),
    )


@router.get("/{run_id}/results", response_model=list[OptimizationResultResponse])
async def get_optimization_results(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OptimizationRun).where(
            OptimizationRun.id == run_id, OptimizationRun.user_id == current_user.id
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Optimization run not found.")

    results = await db.execute(
        select(OptimizationResult)
        .where(OptimizationResult.optimization_run_id == run.id)
        .order_by(OptimizationResult.rank)
    )
    return [
        OptimizationResultResponse(
            rank=r.rank, parameters=r.parameters, net_profit=r.net_profit,
            profit_factor=r.profit_factor, win_rate=r.win_rate, max_drawdown=r.max_drawdown,
            total_trades=r.total_trades, sharpe_ratio=r.sharpe_ratio,
        )
        for r in results.scalars().all()
    ]


async def _run_optimization_wrapper(run_id: str):
    """Wrapper that runs optimization without blocking the event loop."""
    import asyncio
    await _run_optimization_task(run_id)


async def _run_optimization_task(run_id: str):
    """Run optimization: backtest each parameter combination and rank results."""
    import asyncio
    from itertools import product as iterproduct
    from datetime import datetime, timezone
    from app.database import async_session_factory
    from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
    from app.engines.backtest_engine.data_handler import DataHandler
    from app.engines.backtest_engine.backtest_runner import BacktestRunner, BacktestConfig
    from app.engines.backtest_engine.ict_strategy import ICTStrategy
    from app.engines.strategy_engine.base_strategy import StrategyConfig

    try:
        async with async_session_factory() as db:
            result = await db.execute(
                select(OptimizationRun).where(OptimizationRun.id == run_id)
            )
            run = result.scalar_one_or_none()
            if not run:
                return

            run.status = OptimizationStatus.RUNNING
            run.started_at = datetime.now(timezone.utc)
            await db.commit()

            # Load strategy
            strat_result = await db.execute(
                select(Strategy).where(Strategy.id == run.strategy_id)
            )
            strategy_model = strat_result.scalar_one_or_none()
            if not strategy_model:
                run.status = OptimizationStatus.FAILED
                run.error_message = "Strategy not found"
                await db.commit()
                return

            # ─── CACHE CHECK: did anyone already run this exact optimization? ───
            cache_key = _opt_cache_key(
                strategy_model.name, run.parameter_grid,
                run.instrument, run.start_date, run.end_date,
            )
            cached = await _try_cached_optimization(db, cache_key)
            if cached:
                from app.models.optimization import OptimizationResult as _OR
                from datetime import datetime as _dt2, timezone as _tz2
                from loguru import logger as _lg
                _lg.info(f"OPT CACHE HIT: copying {len(cached)} results from cache (cache_key={cache_key[:12]}...)")
                for c in cached:
                    db.add(_OR(
                        optimization_run_id=run.id,
                        rank=c.get("rank", 0),
                        parameters=c.get("parameters", {}),
                        net_profit=c.get("net_profit", 0),
                        profit_factor=c.get("profit_factor", 0),
                        win_rate=c.get("win_rate", 0),
                        max_drawdown=c.get("max_drawdown", 0),
                        total_trades=c.get("total_trades", 0),
                        sharpe_ratio=c.get("sharpe_ratio", 0),
                    ))
                run.status = OptimizationStatus.COMPLETED
                run.progress = 100.0
                run.completed_at = _dt2.now(_tz2.utc)
                await db.commit()
                _lg.info(f"OPT CACHE: done in <1s instead of full grid run")
                return
            # ─── CACHE MISS: proceed with normal optimization ───

            # Use strategy execution timeframe for base data
            base_tf = "1m"  # Always 1m so timeframe optimization works
            
            print(f"OPTIMIZER: Fetching {run.instrument} data {run.start_date} -> {run.end_date} at {base_tf}")
            
            df = await fetch_futures_data(
                instrument=run.instrument,
                start_date=run.start_date,
                end_date=run.end_date,
                interval=base_tf,
                use_polygon=True,
            )

            if df is None or df.empty:
                run.status = OptimizationStatus.FAILED
                run.error_message = "No market data available"
                await db.commit()
                return

            # Build parameter combinations
            param_keys = list(run.parameter_grid.keys())
            param_values = list(run.parameter_grid.values())
            combos = list(iterproduct(*param_values))
            run.total_combinations = len(combos)
            await db.commit()

            all_results = []

            for idx, combo in enumerate(combos):
                params = dict(zip(param_keys, combo))

                try:
                    # Build config with overridden parameters
                    config = StrategyConfig(
                        name=strategy_model.name,
                        instruments=strategy_model.instruments or [run.instrument],
                        primary_timeframe=params.get("primary_timeframe", strategy_model.primary_timeframe or "15m"),
                        execution_timeframe=params.get("execution_timeframe", strategy_model.execution_timeframe or "1m"),
                        higher_timeframes=strategy_model.higher_timeframes or [],
                        risk_reward_ratio=float(params.get("risk_reward_ratio", strategy_model.risk_reward_ratio or 2.0)),
                        stop_loss_type=params.get("stop_loss_type", strategy_model.stop_loss_type or "structure"),
                        stop_loss_ticks=int(params.get("stop_loss_ticks", strategy_model.stop_loss_ticks or 8)),
                        max_contracts=strategy_model.max_contracts or 1,
                        session_filters=strategy_model.session_filters or [],
                        fvg_min_size_ticks=int(params.get("fvg_min_size_ticks", strategy_model.fvg_min_size_ticks or 4)),
                        fvg_max_size_ticks=strategy_model.fvg_max_size_ticks,
                        max_daily_loss=strategy_model.max_daily_loss,
                        max_trades_per_day=strategy_model.max_trades_per_day,
                        use_rsi_filter=bool((strategy_model.rule_tree or {}).get("use_rsi_filter", False)),
                        use_vwap_filter=bool((strategy_model.rule_tree or {}).get("use_vwap_filter", False)),
                    )

                    strategy = ICTStrategy(config, instrument=run.instrument)
                    data_handler = DataHandler(instrument=run.instrument, base_timeframe=base_tf)
                    data_handler.load_from_dataframe(df.reset_index())

                    all_tfs = list(set([config.primary_timeframe, config.execution_timeframe] + config.higher_timeframes))

                    bt_config = BacktestConfig(
                        instrument=run.instrument,
                        start_date=run.start_date,
                        end_date=run.end_date,
                        primary_timeframe=config.primary_timeframe,
                        all_timeframes=all_tfs,
                        initial_capital=100000,
                        commission_per_side=2.50,
                        slippage_ticks=1,
                    )

                    runner = BacktestRunner(strategy, data_handler, bt_config)
                    metrics = await asyncio.to_thread(runner.run)

                    all_results.append({
                        "params": params,
                        "net_profit": metrics.net_profit,
                        "profit_factor": metrics.profit_factor,
                        "win_rate": metrics.win_rate,
                        "max_drawdown": metrics.max_drawdown_pct,
                        "total_trades": metrics.total_trades,
                        "sharpe_ratio": metrics.sharpe_ratio,
                    })

                    print(f"OPTIMIZER: Combo {idx+1}/{len(combos)} done: {metrics.total_trades} trades, ${metrics.net_profit:.0f} profit")

                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    print(f"OPTIMIZER: Combo {idx+1}/{len(combos)} FAILED: {e}")
                    all_results.append({
                        "params": params,
                        "net_profit": 0,
                        "profit_factor": 0,
                        "win_rate": 0,
                        "max_drawdown": 0,
                        "total_trades": 0,
                        "sharpe_ratio": 0,
                    })

                run.completed_combinations = idx + 1
                await db.commit()

            # Sort by optimization metric
            metric_key = run.optimization_metric or "profit_factor"
            reverse = True  # higher is better for all metrics except max_drawdown
            if metric_key == "max_drawdown":
                reverse = False

            all_results.sort(key=lambda x: x.get(metric_key, 0) or 0, reverse=reverse)

            # Store top 20 results
            for rank, r in enumerate(all_results[:20], 1):
                opt_result = OptimizationResult(
                    optimization_run_id=run.id,
                    parameters=r["params"],
                    rank=rank,
                    net_profit=r["net_profit"],
                    profit_factor=r["profit_factor"],
                    win_rate=r["win_rate"],
                    max_drawdown=r["max_drawdown"],
                    total_trades=r["total_trades"],
                    sharpe_ratio=r["sharpe_ratio"],
                )
                db.add(opt_result)

            run.status = OptimizationStatus.COMPLETED
            run.completed_at = datetime.now(timezone.utc)
            await db.commit()
            # Cross-user cache: store these results so the next user with same params gets instant hit
            try:
                await _store_optimization_cache(
                    db, cache_key, strategy_model.name, run.parameter_grid,
                    run.instrument, run.start_date, run.end_date,
                    all_results, getattr(run, "user_id", None),
                )
                logger.info(f"OPT CACHE: stored {len(all_results)} results under {cache_key[:12]}...")
            except Exception as _e:
                logger.warning(f"OPT CACHE: store failed: {_e}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        try:
            async with async_session_factory() as db:
                result = await db.execute(
                    select(OptimizationRun).where(OptimizationRun.id == run_id)
                )
                run = result.scalar_one_or_none()
                if run:
                    run.status = OptimizationStatus.FAILED
                    run.error_message = str(e)
                    await db.commit()
        except:
            pass



@router.post("/{run_id}/apply")
async def apply_optimization_result(
    run_id: str,
    rank: int = 1,
    current_user: User = Depends(require_tier(*live_tiers)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(OptimizationRun).where(
            OptimizationRun.id == run_id, OptimizationRun.user_id == current_user.id
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Optimization run not found.")
    opt_result = await db.execute(
        select(OptimizationResult).where(
            OptimizationResult.optimization_run_id == run.id,
            OptimizationResult.rank == rank,
        )
    )
    best = opt_result.scalar_one_or_none()
    if not best:
        raise HTTPException(status_code=404, detail=f"No result at rank {rank}.")
    strat_result = await db.execute(
        select(Strategy).where(Strategy.id == run.strategy_id)
    )
    strategy = strat_result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")
    params = best.parameters
    if "risk_reward_ratio" in params:
        strategy.risk_reward_ratio = float(params["risk_reward_ratio"])
    if "stop_loss_ticks" in params:
        strategy.stop_loss_ticks = int(params["stop_loss_ticks"])
    if "fvg_min_size_ticks" in params:
        strategy.fvg_min_size_ticks = int(params["fvg_min_size_ticks"])
    if "primary_timeframe" in params:
        strategy.primary_timeframe = params["primary_timeframe"]
    if "execution_timeframe" in params:
        strategy.execution_timeframe = params["execution_timeframe"]
    if "stop_loss_type" in params:
        strategy.stop_loss_type = params["stop_loss_type"]
    await db.commit()
    return {"message": f"Applied rank {rank} parameters to strategy", "parameters": params}


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_optimization(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    run = await db.get(OptimizationRun, run_id)
    if not run or str(run.user_id) != str(current_user.id):
        raise HTTPException(status_code=404, detail="Not found")
    # Delete results first
    await db.execute(
        delete(OptimizationResult).where(OptimizationResult.optimization_run_id == run.id)
    )
    await db.delete(run)
    await db.commit()
