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
from app.core.auth import require_2fa_when_paid as get_current_user, require_tier
from loguru import logger

router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription

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
    # failure_reason surfaces the persisted error_message so the UI can show
    # *why* a run failed instead of a silent red dot. Alias kept as both names
    # for frontend convenience.
    error_message: Optional[str] = None
    failure_reason: Optional[str] = None
    progress: float = 0.0
    eta_seconds: Optional[float] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


def _opt_progress(run) -> float:
    """Percent complete. The model has no progress column, so derive it from
    completed/total (and force 100 on a completed run)."""
    st = run.status.value if hasattr(run.status, "value") else str(run.status)
    if st == "completed":
        return 100.0
    tot = run.total_combinations or 0
    if tot <= 0:
        return 0.0
    return round(min(100.0, (run.completed_combinations or 0) / tot * 100.0), 1)


def _opt_eta(run) -> Optional[float]:
    """Seconds remaining, extrapolated from measured per-combo time. None until
    at least one combo has finished; 0 once the run is terminal."""
    st = run.status.value if hasattr(run.status, "value") else str(run.status)
    if st in ("completed", "failed"):
        return 0.0
    done = run.completed_combinations or 0
    tot = run.total_combinations or 0
    if not run.started_at or done <= 0 or tot <= 0:
        return None
    from datetime import datetime as _dt, timezone as _tz
    started = run.started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=_tz.utc)
    elapsed = (_dt.now(_tz.utc) - started).total_seconds()
    return round(elapsed / done * (tot - done), 0)


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
            error_message=r.error_message, failure_reason=r.error_message,
            progress=_opt_progress(r), eta_seconds=_opt_eta(r),
            started_at=r.started_at.isoformat() if r.started_at else None,
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
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
    if data.end_date <= data.start_date:
        raise HTTPException(status_code=400, detail="end_date must be after start_date.")
    if data.end_date - data.start_date > max_range:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 3 years.")

    # ── Validate the parameter grid BEFORE queueing so the user gets a clear
    #    error synchronously instead of a silent worker failure. ──
    grid = data.parameter_grid or {}
    if not isinstance(grid, dict) or not grid:
        raise HTTPException(status_code=400, detail="parameter_grid must be a non-empty object.")
    # Known optimizable parameters understood by the backtest engine
    # (see _run_one_combo in _run_optimization_task).
    KNOWN_PARAMS = {
        "risk_reward_ratio", "stop_loss_ticks", "fvg_min_size_ticks",
        "primary_timeframe", "execution_timeframe", "stop_loss_type",
        "breakeven_at_r",
    }
    unknown = [k for k in grid.keys() if k not in KNOWN_PARAMS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(f"Unknown optimization parameter(s): {sorted(unknown)}. "
                    f"Supported: {sorted(KNOWN_PARAMS)}."),
        )
    for k, v in grid.items():
        if not isinstance(v, (list, tuple)) or len(v) == 0:
            raise HTTPException(
                status_code=400,
                detail=f"parameter_grid[{k}] must be a non-empty list of values to test.",
            )
    # Custom strategies must carry the base fields the engine reads. A brand-new
    # draft with an empty rule_tree is fine (ICT defaults apply), but the core
    # numeric fields must be present/sane or every combo silently zeroes out.
    if strategy.risk_reward_ratio is None or float(strategy.risk_reward_ratio) <= 0:
        raise HTTPException(
            status_code=400,
            detail="Strategy is missing a valid risk_reward_ratio. Open it in the builder and save before optimizing.",
        )

    from itertools import product
    combos = list(product(*grid.values()))
    total  = len(combos)
    if total == 0 or total > 2000:
        raise HTTPException(
            status_code=400,
            detail=f"parameter_grid expands to {total} combinations (must be 1-2000).",
        )

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


@router.get("/{run_id}", response_model=OptimizationRunResponse)
async def get_optimization_run(
    run_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Single optimization run incl. status + failure_reason. Previously absent,
    so a bare GET /optimization/{id} matched the DELETE path and returned 405."""
    result = await db.execute(
        select(OptimizationRun, Strategy.name)
        .join(Strategy, OptimizationRun.strategy_id == Strategy.id, isouter=True)
        .where(OptimizationRun.id == run_id, OptimizationRun.user_id == current_user.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Optimization run not found.")
    run, strategy_name = row
    return OptimizationRunResponse(
        id=str(run.id), strategy_id=str(run.strategy_id) if run.strategy_id else "",
        strategy_name=strategy_name or "(deleted strategy)",
        instrument=run.instrument,
        status=run.status.value, total_combinations=run.total_combinations,
        completed_combinations=run.completed_combinations, created_at=run.created_at.isoformat(),
        error_message=run.error_message, failure_reason=run.error_message,
        progress=_opt_progress(run), eta_seconds=_opt_eta(run),
        started_at=run.started_at.isoformat() if run.started_at else None,
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
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
            logger.info(
                f"[OPT START] run={run.id} strategy={run.strategy_id} instrument={run.instrument} "
                f"grid={run.parameter_grid} metric={run.optimization_metric} "
                f"-- SIMULATION MODE: writes only to optimization_runs/optimization_results; "
                f"never to email/pick/notification/account tables"
            )

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

            all_results = [None] * len(combos)  # preserve index order

            # ── Pre-build a SHARED DataHandler used by every combo ──────────
            # Loading + resampling 1 year of 1m data is the single most
            # expensive op per combo (~5-10 sec). Doing it 48× = 4-8 min of
            # pure waste since every combo has the same base data + the same
            # date range. Compute the superset of timeframes any combo could
            # need (primary/execution timeframes vary in the grid; higher
            # timeframes are fixed by the strategy) and pre-build once.
            possible_tfs = set([strategy_model.primary_timeframe or "15m",
                                 strategy_model.execution_timeframe or "1m"])
            possible_tfs.update(strategy_model.higher_timeframes or [])
            # also include any TFs that appear as combo parameters
            for combo in combos:
                params = dict(zip(param_keys, combo))
                if "primary_timeframe" in params: possible_tfs.add(params["primary_timeframe"])
                if "execution_timeframe" in params: possible_tfs.add(params["execution_timeframe"])
            shared_data_handler = DataHandler(instrument=run.instrument, base_timeframe=base_tf)
            shared_data_handler.load_from_dataframe(df.reset_index())
            shared_data_handler.build_timeframes(list(possible_tfs))
            logger.info(f"OPT: shared DataHandler pre-built {len(possible_tfs)} timeframes — combos will reuse")

            # ── Parallel batch runner ───────────────────────────────────────
            # Each combo gets its own strategy + runner but SHARES the
            # data_handler (read-only — get_bars_up_to is pure-read).
            # ── Real multi-core parallel execution (process pool) ────────────
            # Threads cannot parallelize CPU-bound backtests (Python GIL) — that
            # is why this used to peg ONE core and crawl for 20-90 min. A process
            # pool runs combos on separate cores; each worker rebuilds the
            # DataHandler ONCE (initializer) then reuses it. Progress + running
            # best update PER COMBO (as_completed) so the UI never sits at 0%.
            import time as _time, concurrent.futures as _cf, multiprocessing as _mp, os as _os
            from app.engines.optimization_engine import opt_worker as _ow

            metric_key = run.optimization_metric or "profit_factor"
            _strat = {
                "name": strategy_model.name, "instruments": strategy_model.instruments,
                "primary_timeframe": strategy_model.primary_timeframe,
                "execution_timeframe": strategy_model.execution_timeframe,
                "higher_timeframes": strategy_model.higher_timeframes,
                "risk_reward_ratio": strategy_model.risk_reward_ratio,
                "stop_loss_type": strategy_model.stop_loss_type,
                "stop_loss_ticks": strategy_model.stop_loss_ticks,
                "max_contracts": strategy_model.max_contracts,
                "session_filters": strategy_model.session_filters,
                "fvg_min_size_ticks": strategy_model.fvg_min_size_ticks,
                "fvg_max_size_ticks": strategy_model.fvg_max_size_ticks,
                "max_daily_loss": strategy_model.max_daily_loss,
                "max_trades_per_day": strategy_model.max_trades_per_day,
                "breakeven_at_r": getattr(strategy_model, "breakeven_at_r", None),
                "breakeven_mode": getattr(strategy_model, "breakeven_mode", None),
                "rule_tree": strategy_model.rule_tree or {},
            }
            _df_records = df.reset_index().to_dict("list")

            # ── Deduplicate functionally-identical combos ────────────────────
            # A grid dimension can be a no-op for a given strategy: e.g.
            # stop_loss_ticks does NOTHING when stop_loss_type='structure' (the
            # stop is derived from market structure, not a tick distance), so
            # those combos produce byte-identical results. Build a
            # result-affecting signature from the ACTUAL grid params (dropping
            # ONLY the proven no-op), run each unique config ONCE, and replicate
            # its result to the duplicates. Big speedup, zero change to results.
            _combo_params = [dict(zip(param_keys, c)) for c in combos]
            def _sig(p):
                st = p.get("stop_loss_type", _strat["stop_loss_type"] or "structure")
                items = []
                for k in param_keys:
                    if k == "stop_loss_ticks" and st != "ticks":
                        continue  # tick distance is irrelevant for a structure stop
                    items.append((k, p.get(k)))
                return tuple(items)
            _groups = {}   # rep combo index -> [all member combo indices]
            _sig_rep = {}
            for _i, _p in enumerate(_combo_params):
                _key = _sig(_p)
                if _key not in _sig_rep:
                    _sig_rep[_key] = _i; _groups[_i] = []
                _groups[_sig_rep[_key]].append(_i)
            _reps = list(_groups.keys())

            # Stable JSON-storable key per distinct config so checkpointed results
            # can be matched back on resume (the tuple sig itself isn't JSON-safe).
            def _sigkey(_p):
                return repr(_sig(_p))

            _loop = asyncio.get_event_loop()
            _t0 = _time.monotonic()
            done_count = 0
            best = None
            _failed = 0

            # ── RESUME from checkpoint ───────────────────────────────────────
            # run.partial_results holds {sig, result} for every distinct config
            # that already finished (persisted AS it completed). After a worker
            # death / backend restart we replay those and run ONLY what's left,
            # so a restart never loses computed work.
            _done_map = {}
            for _e in (run.partial_results or []):
                if isinstance(_e, dict) and _e.get("sig") is not None:
                    _done_map[_e["sig"]] = _e.get("result") or {}
            _resuming = len(_done_map) > 0
            if _resuming:
                run.status = OptimizationStatus.RESUMED
                run.last_heartbeat_at = datetime.now(timezone.utc)
                await db.commit()
                logger.info(f"[OPT RESUME] run={run.id} resuming from checkpoint: "
                            f"{len(_done_map)} distinct configs already done, running the remainder")
            _to_run = []
            for _ridx in _reps:
                _sk = _sigkey(_combo_params[_ridx])
                if _sk in _done_map:
                    res = _done_map[_sk]
                    members = _groups.get(_ridx, [_ridx])
                    for _m in members:
                        mres = dict(res); mres["params"] = _combo_params[_m]
                        all_results[_m] = mres
                    done_count += len(members)
                    if res.get("_error"):
                        _failed += len(members)
                    elif best is None or (res.get(metric_key, 0) or 0) > (best.get(metric_key, 0) or 0):
                        best = res
                else:
                    _to_run.append(_ridx)
            run.completed_combinations = done_count
            await db.commit()

            _n_workers = min(max(1, len(_to_run)), max(1, (_os.cpu_count() or 2) - 1))
            _ctx = _mp.get_context("spawn")
            _pool = _cf.ProcessPoolExecutor(
                max_workers=_n_workers, mp_context=_ctx,
                initializer=_ow.init_worker,
                initargs=(_df_records, run.instrument, base_tf, list(possible_tfs)),
            )
            logger.info(f"[OPT] run={run.id} {len(combos)} combos -> {len(_reps)} distinct configs "
                        f"({len(combos)-len(_reps)} duplicate no-op skipped); "
                        f"{len(_reps)-len(_to_run)} from checkpoint, {len(_to_run)} to run "
                        f"across {_n_workers} processes")
            _new_done = 0
            try:
                _futs = [
                    _loop.run_in_executor(_pool, _ow.run_combo, _ridx,
                                          dict(zip(param_keys, combos[_ridx])),
                                          _strat, run.start_date, run.end_date)
                    for _ridx in _to_run
                ]
                for _fut in asyncio.as_completed(_futs):
                    ridx, res = await _fut
                    _sk = _sigkey(_combo_params[ridx])
                    members = _groups.get(ridx, [ridx])
                    for _m in members:                       # replicate to duplicate param sets
                        mres = dict(res); mres["params"] = _combo_params[_m]
                        all_results[_m] = mres
                    done_count += len(members); _new_done += 1
                    if res.get("_error"):
                        _failed += len(members)
                        logger.error(f"[OPT COMBO-FAIL] run={run.id} config={res.get('params')} "
                                     f"err={res.get('_error')}")
                    elif best is None or (res.get(metric_key, 0) or 0) > (best.get(metric_key, 0) or 0):
                        best = res
                    # ── CHECKPOINT this distinct config (reassign list so the JSON
                    # column is flagged dirty; .append would not commit). ──
                    run.partial_results = (run.partial_results or []) + [{"sig": _sk, "result": res}]
                    run.completed_combinations = done_count
                    run.last_heartbeat_at = datetime.now(timezone.utc)
                    await db.commit()
                    _elapsed = _time.monotonic() - _t0
                    _eta = int(_elapsed / _new_done * (len(_to_run) - _new_done)) if (_new_done and len(_to_run) > _new_done) else 0
                    logger.info(
                        f"[OPT PROGRESS] run={run.id} {done_count}/{len(combos)} "
                        f"({done_count*100//max(1,len(combos))}%) [checkpoint saved] config={res.get('params')} "
                        f"{metric_key}={res.get(metric_key)} best_{metric_key}={best.get(metric_key) if best else None} "
                        f"failed={_failed} eta~{_eta}s"
                    )
            finally:
                _pool.shutdown(wait=False, cancel_futures=True)
            logger.info(f"[OPT] run={run.id} all {len(combos)} combos done in "
                        f"{int(_time.monotonic()-_t0)}s ({_failed} failed)")

            # If EVERY combo errored, the run is a real failure — surface it
            # instead of "completing" with a grid of zeroes.
            errored = [r for r in all_results if r and r.get("_error")]
            if all_results and len(errored) == len(all_results):
                run.status = OptimizationStatus.FAILED
                run.error_message = (
                    f"All {len(all_results)} parameter combinations failed. "
                    f"First error: {errored[0].get('_error')}"
                )[:480]
                run.completed_at = datetime.now(timezone.utc)
                await db.commit()
                logger.error(f"OPT run {run_id}: all combos failed — {run.error_message}")
                return

            # Sort by optimization metric
            metric_key = run.optimization_metric or "profit_factor"
            reverse = True  # higher is better for all metrics except max_drawdown
            if metric_key == "max_drawdown":
                reverse = False

            all_results.sort(key=lambda x: x.get(metric_key, 0) or 0, reverse=reverse)
            _best = all_results[0] if all_results else None
            if _best:
                logger.info(
                    f"[OPT BEST] run={run.id} metric={metric_key} best_params={_best.get('params')} "
                    f"best_{metric_key}={_best.get(metric_key)} net_profit={_best.get('net_profit')} "
                    f"trades={_best.get('total_trades')}"
                )

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
    logger.info(f"[OPT APPLY] run={run_id} rank={rank} applied params to strategy {run.strategy_id}: {params}")
    return {"message": f"Applied rank {rank} parameters to strategy", "parameters": params}


@router.post("/{run_id}/retry", response_model=OptimizationRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def retry_optimization(
    run_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_tier(*live_tiers)),
    db: AsyncSession = Depends(get_db),
):
    """Re-queue a FAILED optimization run (e.g. one killed by a backend restart).
    Clears partial results + error, resets counters, and relaunches the worker.
    Only FAILED runs can be retried."""
    result = await db.execute(
        select(OptimizationRun, Strategy.name)
        .join(Strategy, OptimizationRun.strategy_id == Strategy.id, isouter=True)
        .where(OptimizationRun.id == run_id, OptimizationRun.user_id == current_user.id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Optimization run not found.")
    run, strategy_name = row
    cur_status = run.status.value if hasattr(run.status, "value") else str(run.status)
    if cur_status != "failed":
        raise HTTPException(status_code=409, detail=f"Only failed runs can be retried (this one is {cur_status}).")
    if run.strategy_id is None:
        raise HTTPException(status_code=409, detail="The strategy for this run was deleted; cannot retry.")

    # Clear any partial results + reset the run to QUEUED.
    await db.execute(delete(OptimizationResult).where(OptimizationResult.optimization_run_id == run.id))
    run.status = OptimizationStatus.QUEUED
    run.partial_results = []        # fresh re-run -> drop old checkpoint so it won't resume
    run.completed_combinations = 0
    run.error_message = None
    run.completed_combinations = 0
    run.error_message = None
    run.started_at = None
    run.completed_at = None
    await db.commit()

    background_tasks.add_task(_run_optimization_wrapper, str(run.id))
    logger.info(f"OPT retry: re-queued run {run_id}")

    return OptimizationRunResponse(
        id=str(run.id), strategy_id=str(run.strategy_id) if run.strategy_id else "",
        strategy_name=strategy_name or "(deleted strategy)",
        instrument=run.instrument,
        status=run.status.value, total_combinations=run.total_combinations,
        completed_combinations=run.completed_combinations, created_at=run.created_at.isoformat(),
        error_message=None, failure_reason=None,
        progress=_opt_progress(run),
        started_at=None, completed_at=None,
    )


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
