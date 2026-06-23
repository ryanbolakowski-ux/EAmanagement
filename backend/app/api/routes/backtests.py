import os
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from datetime import datetime
import asyncio
from typing import Optional
import math
from loguru import logger

from app.database import get_db
from app.models.user import User
from app.models.strategy import Strategy
from app.models.backtest import BacktestRun, BacktestStatus, BacktestMetrics, BacktestTrade
from app.core.auth import require_2fa_when_paid as get_current_user


def _safe_float(v):
    """Coerce DB floats to JSON-safe values (None/inf/nan -> 0.0)."""
    if v is None:
        return 0.0
    if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
        return 0.0
    return v


def _iso_or_none(dt):
    return dt.isoformat() if dt is not None else None


from sqlalchemy import text as _sql_text


def _assess_quality(pf, win_rate, max_dd_pct, total_trades, expectancy):
    """Bug 11 guardrail: a strategy is only 'good' when it clears ALL of
    profit_factor, win_rate, max_drawdown, trade count and expectancy. Tiny
    samples are never 'good' regardless of headline numbers."""
    reasons = []
    MIN_TRADES = 30
    if (total_trades or 0) < MIN_TRADES:
        reasons.append("sample too small (" + str(total_trades or 0) + " trades, need >= " + str(MIN_TRADES) + ")")
    if (pf or 0) < 1.3:
        reasons.append("profit factor low (" + format(pf or 0, ".2f") + " < 1.3)")
    if (win_rate or 0) < 0.40:
        reasons.append("win rate low (" + format((win_rate or 0) * 100, ".0f") + "% < 40%)")
    if abs(max_dd_pct or 0) > 25:
        reasons.append("max drawdown high (" + format(abs(max_dd_pct or 0), ".0f") + "% > 25%)")
    if (expectancy or 0) <= 0:
        reasons.append("expectancy not positive")
    return {
        "is_good": len(reasons) == 0,
        "small_sample": (total_trades or 0) < MIN_TRADES,
        "reasons": reasons,
    }



router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription


class BacktestRequest(BaseModel):
    strategy_id: str
    instrument: str = "ES"
    start_date: datetime
    end_date: datetime
    timeframe: str = "15m"
    initial_capital: float = 100_000.0
    commission_per_side: float = 2.25
    slippage_ticks: int = 1
    risk_per_trade_pct: float = 1.0
    trailing_drawdown: float = 0.0
    daily_loss_limit: float = 0.0
    # Optional per-run override of the strategy's break-even management.
    # None -> use the strategy's configured breakeven_at_r.
    breakeven_at_r: Optional[float] = None
    breakeven_mode: Optional[str] = None


class BacktestRunResponse(BaseModel):
    id: str
    strategy_id: str
    strategy_name: str
    instrument: str
    start_date: str
    end_date: str
    status: str
    created_at: str
    completed_at: Optional[str] = None
    progress: float = 0.0


class MetricsResponse(BaseModel):
    breakeven_trades: int = 0
    effective_win_rate: float = 0.0
    total_trades: int
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float
    net_profit: float
    profit_factor: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: Optional[float]
    avg_rr: float
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    equity_curve: list
    monthly_returns: dict


@router.post("/", response_model=BacktestRunResponse, status_code=status.HTTP_202_ACCEPTED)
async def run_backtest(
    data: BacktestRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Validate strategy ownership
    result = await db.execute(
        select(Strategy).where(Strategy.id == data.strategy_id, Strategy.user_id == current_user.id)
    )
    strategy = result.scalar_one_or_none()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found.")

    # Validate date range — free trial capped at 1 year, paid tiers at 3 years
    from datetime import timedelta
    is_free_trial = current_user.subscription_tier == "free_trial"
    max_days = 365 if is_free_trial else 365 * 3
    if data.end_date - data.start_date > timedelta(days=max_days):
        limit_label = "1 year" if is_free_trial else "3 years"
        raise HTTPException(status_code=400, detail=f"Date range cannot exceed {limit_label} on your plan.")

    run = BacktestRun(
        strategy_id=strategy.id,
        user_id=current_user.id,
        instrument=data.instrument,
        start_date=data.start_date,
        end_date=data.end_date,
        timeframe=strategy.primary_timeframe or data.timeframe,
        initial_capital=data.initial_capital,
        commission_per_side=data.commission_per_side,
        slippage_ticks=data.slippage_ticks,
        risk_per_trade_pct=data.risk_per_trade_pct,
        trailing_drawdown=data.trailing_drawdown,
        daily_loss_limit=data.daily_loss_limit,
        strategy_params_snapshot={
            "primary_timeframe": strategy.primary_timeframe,
            "execution_timeframe": strategy.execution_timeframe,
            "risk_reward_ratio": strategy.risk_reward_ratio,
            "stop_loss_ticks": strategy.stop_loss_ticks,
            "fvg_min_size_ticks": strategy.fvg_min_size_ticks,
            "session_filters": strategy.session_filters,
            "breakeven_at_r": (
                data.breakeven_at_r if data.breakeven_at_r is not None
                else (getattr(strategy, "breakeven_at_r", None) or 0.0)
            ),
            "breakeven_mode": (
                data.breakeven_mode if data.breakeven_mode is not None
                else (getattr(strategy, "breakeven_mode", None) or "off")
            ),
            # SNAPSHOT-V2: full, self-contained config so two runs can be diffed
            # authoritatively (this is what makes 'nearly identical but not' explainable).
            "rule_tree": (getattr(strategy, "rule_tree", None) or {}),
            "engine_version": str((getattr(strategy, "rule_tree", None) or {}).get("engine_version", "v1") or "v1"),
            "use_vwap_filter": bool((getattr(strategy, "rule_tree", None) or {}).get("use_vwap_filter", False)),
            "take_profit_mode": (getattr(strategy, "rule_tree", None) or {}).get("take_profit_mode", "auto"),
            "instruments": (getattr(strategy, "instruments", None) or []),
            "higher_timeframes": (getattr(strategy, "higher_timeframes", None) or []),
            "stop_loss_type": getattr(strategy, "stop_loss_type", None),
            "fvg_max_size_ticks": getattr(strategy, "fvg_max_size_ticks", None),
            "max_contracts": getattr(strategy, "max_contracts", None),
            "max_daily_loss": getattr(strategy, "max_daily_loss", None),
            "max_trades_per_day": getattr(strategy, "max_trades_per_day", None),
            "initial_capital": data.initial_capital,
            "commission_per_side": data.commission_per_side,
            "slippage_ticks": data.slippage_ticks,
            "risk_per_trade_pct": data.risk_per_trade_pct,
            "trailing_drawdown": data.trailing_drawdown,
            "daily_loss_limit": data.daily_loss_limit,
            "engine_code_version": "2026-06-19-rangeTP+vwap+be",
        },
        status=BacktestStatus.QUEUED,
    )
    db.add(run)
    await db.commit()

    # Queue backtest task
    background_tasks.add_task(_run_backtest_task, str(run.id))

    return BacktestRunResponse(
        id=str(run.id),
        strategy_id=str(run.strategy_id),
        strategy_name=strategy.name,
        instrument=run.instrument,
        start_date=run.start_date.isoformat(),
        end_date=run.end_date.isoformat(),
        status=run.status.value,
        created_at=run.created_at.isoformat(),
        progress=0.0,
    )


@router.get("/", response_model=list[BacktestRunResponse])
async def list_backtests(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import redis as _redis
    _r = _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=False, db=0)
    result = await db.execute(
        select(BacktestRun, Strategy.name)
        .join(Strategy, BacktestRun.strategy_id == Strategy.id)
        .where(BacktestRun.user_id == current_user.id)
        .order_by(BacktestRun.created_at.desc())
    )
    responses = []
    for r, strategy_name in result.all():
        progress = r.progress or 0.0
        if r.status in (BacktestStatus.RUNNING, BacktestStatus.QUEUED):
            live = _r.get(f"backtest:{r.id}:progress")
            if live:
                progress = float(live)
        responses.append(BacktestRunResponse(
            id=str(r.id), strategy_id=str(r.strategy_id), strategy_name=strategy_name,
            instrument=r.instrument,
            start_date=r.start_date.isoformat(), end_date=r.end_date.isoformat(),
            status=r.status.value, created_at=r.created_at.isoformat(),
            completed_at=r.completed_at.isoformat() if r.completed_at else None,
            progress=progress,
        ))
    return responses


@router.get("/{backtest_id}/metrics", response_model=MetricsResponse)
async def get_backtest_metrics(
    backtest_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BacktestRun).where(BacktestRun.id == backtest_id, BacktestRun.user_id == current_user.id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest run not found.")
    if run.status != BacktestStatus.COMPLETED:
        raise HTTPException(status_code=400, detail=f"Backtest is {run.status.value}, not completed.")

    m_result = await db.execute(select(BacktestMetrics).where(BacktestMetrics.backtest_run_id == run.id))
    m = m_result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Metrics not yet calculated.")

    safe_float = _safe_float
    # Expectancy = average net P&L per trade. Computed from net_profit/total
    # (unambiguous) instead of left null. Surfaced so the UI stops showing blank.
    _tt = m.total_trades or 0
    _expectancy = (_safe_float(m.net_profit) / _tt) if _tt > 0 else 0.0

    return MetricsResponse(
        total_trades=m.total_trades,
        winning_trades=int(getattr(m, "winning_trades", 0) or 0), losing_trades=int(getattr(m, "losing_trades", 0) or 0),
        win_rate=safe_float(m.win_rate), net_profit=safe_float(m.net_profit),
        profit_factor=safe_float(m.profit_factor), max_drawdown=safe_float(m.max_drawdown), max_drawdown_pct=safe_float(m.max_drawdown_pct),
        sharpe_ratio=safe_float(m.sharpe_ratio), avg_rr=safe_float(m.avg_rr),
        breakeven_trades=int(getattr(m, "breakeven_trades", 0) or 0),
        effective_win_rate=safe_float(getattr(m, "effective_win_rate", 0.0)),
        expectancy=round(_expectancy, 2),
        avg_win=safe_float(getattr(m, "avg_win", 0.0)), avg_loss=safe_float(getattr(m, "avg_loss", 0.0)),
        equity_curve=m.equity_curve or [], monthly_returns=m.monthly_returns or {},
    )


@router.delete("/{backtest_id}", status_code=204)
async def delete_backtest(
    backtest_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(BacktestRun).where(BacktestRun.id == backtest_id, BacktestRun.user_id == current_user.id)
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest not found.")
    # Delete related records first
    await db.execute(select(BacktestTrade).where(BacktestTrade.backtest_run_id == run.id))
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(BacktestTrade).where(BacktestTrade.backtest_run_id == run.id))
    await db.execute(sa_delete(BacktestMetrics).where(BacktestMetrics.backtest_run_id == run.id))
    await db.delete(run)
    await db.commit()


async def _record_backtest_failure(run_id: str, msg: str):
    """Persist a real failure reason on a FRESH session, so a poisoned
    transaction on the main session can never swallow the error. Idempotent;
    never overwrites a COMPLETED run."""
    from app.database import async_session_factory
    try:
        async with async_session_factory() as _db:
            r = (await _db.execute(select(BacktestRun).where(BacktestRun.id == run_id))).scalar_one_or_none()
            if r is not None and r.status != BacktestStatus.COMPLETED:
                r.status = BacktestStatus.FAILED
                r.error_message = (msg or "Backtest failed (no detail captured)")[:1000]
                r.completed_at = datetime.utcnow()
                await _db.commit()
    except Exception:
        logger.error(f"[backtest] could not record failure for run={run_id}")


async def _run_backtest_task(backtest_run_id: str):
    """
    Runs the backtest inline (background task).
    Fetches real market data, runs the strategy engine, and stores results.
    """
    from app.database import async_session_factory
    from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
    from app.engines.backtest_engine.data_handler import DataHandler
    from app.engines.backtest_engine.backtest_runner import BacktestRunner, BacktestConfig
    from app.engines.backtest_engine.ict_strategy import ICTStrategy
    from app.engines.strategy_engine.base_strategy import StrategyConfig

    try:
        async with async_session_factory() as db:
            result = await db.execute(
                select(BacktestRun).where(BacktestRun.id == backtest_run_id)
            )
            run = result.scalar_one_or_none()
            if not run:
                return

            # Mark as running
            run.status = BacktestStatus.RUNNING
            run.started_at = datetime.utcnow()
            run.progress = 5.0
            await db.commit()
            logger.info(f"[backtest] run={run.id} START strategy={run.strategy_id} "
                        f"instrument={run.instrument} tf={run.timeframe}")

            # Check if user is on a paid tier for Polygon data
            user_result = await db.execute(
                select(User).where(User.id == run.user_id)
            )
            bt_user = user_result.scalar_one_or_none()
            use_polygon = bt_user and bt_user.subscription_tier in ("tier_1", "tier_2", "tier_3", "tier_4", "tier_5")

            # Load strategy config
            strat_result = await db.execute(
                select(Strategy).where(Strategy.id == run.strategy_id)
            )
            strategy_model = strat_result.scalar_one_or_none()
            if not strategy_model:
                run.status = BacktestStatus.FAILED
                run.error_message = "Strategy not found (deleted, archived, or wrong id)"
                run.completed_at = datetime.utcnow()
                await db.commit()
                return

            # Options strategies follow a different code path (chain pulls,
            # Black-Scholes pricing, daily option-aggs from Polygon).
            if getattr(strategy_model, "options_mode", None):
                try:
                    await _run_options_backtest(run, strategy_model, db)
                except Exception as e:
                    import traceback; traceback.print_exc()
                    logger.error(f"[backtest] run={run.id} options path failed: {type(e).__name__}: {e}")
                    run.status = BacktestStatus.FAILED
                    run.error_message = f"Options backtest error: {type(e).__name__}: {e}"[:1000]
                    run.completed_at = datetime.utcnow()
                    await db.commit()
                return

            try:
                # Fetch real market data (safe retry: data fetch is the one
                # genuinely transient step — network / provider hiccup).
                df = None
                _fetch_err = None
                for _attempt in range(2):
                    try:
                        df = await fetch_futures_data(
                            instrument=run.instrument,
                            start_date=run.start_date,
                            end_date=run.end_date,
                            interval=strategy_model.execution_timeframe or "1m",
                            use_polygon=use_polygon,
                        )
                        if df is not None and not df.empty:
                            break
                    except Exception as _fe:
                        _fetch_err = _fe
                        logger.warning(f"[backtest] run={run.id} data-fetch attempt {_attempt+1} failed: {_fe}")
                    if _attempt == 0:
                        await asyncio.sleep(2)

                run.progress = 20.0
                await db.commit()

                if df is None or df.empty:
                    _ex = strategy_model.execution_timeframe or "1m"
                    _why = (f": {type(_fetch_err).__name__}: {_fetch_err}" if _fetch_err else "")
                    run.status = BacktestStatus.FAILED
                    run.error_message = (f"No market data for {run.instrument} "
                                         f"{run.start_date:%Y-%m-%d}..{run.end_date:%Y-%m-%d} "
                                         f"(tf={_ex}){_why}")[:1000]
                    run.completed_at = datetime.utcnow()
                    await db.commit()
                    logger.warning(f"[backtest] run={run.id} NO DATA -> {run.error_message}")
                    return
                # PROFILE-V1 + SNAPSHOT-V2: record actual data coverage + start engine timer.
                import time as _tprof
                logger.info(f"[PROFILE] run={run.id} data_loaded rows={len(df)} "
                            f"range={df.index[0]}..{df.index[-1]}")
                try:
                    run.strategy_params_snapshot = {**(run.strategy_params_snapshot or {}),
                        "data_rows": int(len(df)), "data_first_ts": str(df.index[0]),
                        "data_last_ts": str(df.index[-1])}
                    await db.commit()
                except Exception:
                    pass
                _prof_engine0 = _tprof.perf_counter()

                # Build strategy
                config = StrategyConfig(
                    name=strategy_model.name,
                    instruments=strategy_model.instruments or [run.instrument],
                    primary_timeframe=strategy_model.primary_timeframe or "15m",
                    execution_timeframe=strategy_model.execution_timeframe or "1m",
                    higher_timeframes=strategy_model.higher_timeframes or [],
                    risk_reward_ratio=strategy_model.risk_reward_ratio or 2.0,
                    stop_loss_type=strategy_model.stop_loss_type or "structure",
                    stop_loss_ticks=strategy_model.stop_loss_ticks,
                    max_contracts=strategy_model.max_contracts or 1,
                    session_filters=strategy_model.session_filters or [],
                    fvg_min_size_ticks=strategy_model.fvg_min_size_ticks or 4,
                    fvg_max_size_ticks=strategy_model.fvg_max_size_ticks,
                    max_daily_loss=strategy_model.max_daily_loss,
                    max_trades_per_day=strategy_model.max_trades_per_day,
                    use_rsi_filter=bool((strategy_model.rule_tree or {}).get("use_rsi_filter", False)),
                    use_vwap_filter=bool((strategy_model.rule_tree or {}).get("use_vwap_filter", False)),
                )
                # RULE-TREE-PLUMB-V1: carry the full rule_tree into the engine
                # (engine_version / ict_setup / compiled rules). Without this the
                # v2 dispatch never fires and every strategy runs the generic model.
                config.rule_tree = strategy_model.rule_tree or {}
                config.take_profit_mode = (strategy_model.rule_tree or {}).get("take_profit_mode", "auto")  # RANGE-TP-V1

                strategy = ICTStrategy(config, instrument=run.instrument)

                # Build data handler
                data_handler = DataHandler(instrument=run.instrument, base_timeframe=strategy_model.execution_timeframe or "1m")
                data_handler.load_from_dataframe(df.reset_index())

                all_tfs = list(set([config.primary_timeframe, config.execution_timeframe] + config.higher_timeframes))

                _be_at_r = (run.strategy_params_snapshot or {}).get("breakeven_at_r")
                if _be_at_r is None:
                    _be_at_r = getattr(strategy_model, "breakeven_at_r", None) or 0.0
                _be_mode = (run.strategy_params_snapshot or {}).get("breakeven_mode")
                if _be_mode is None:
                    _be_mode = getattr(strategy_model, "breakeven_mode", None) or "off"
                bt_config = BacktestConfig(
                    instrument=run.instrument,
                    start_date=run.start_date,
                    end_date=run.end_date,
                    primary_timeframe=config.primary_timeframe,
                    all_timeframes=all_tfs,
                    initial_capital=run.initial_capital,
                    commission_per_side=run.commission_per_side,
                    slippage_ticks=run.slippage_ticks,
                    risk_per_trade_pct=run.risk_per_trade_pct,
                    trailing_drawdown=run.trailing_drawdown,
                    daily_loss_limit=run.daily_loss_limit,
                    breakeven_at_r=float(_be_at_r or 0.0),
                    breakeven_mode=str(_be_mode or "off"),
                )

                run.progress = 40.0
                await db.commit()

                import redis as _redis
                _r = _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=False, db=0)
                def _progress_cb(pct):
                    _r.set(f"backtest:{run.id}:progress", str(pct), ex=3600)
                runner = BacktestRunner(strategy, data_handler, bt_config, progress_callback=_progress_cb)
                metrics = await asyncio.to_thread(runner.run)
                logger.info(f"[PROFILE] run={run.id} engine_run={_tprof.perf_counter()-_prof_engine0:.1f}s "
                            f"trades={getattr(metrics,'total_trades',0)}")

                run.progress = 80.0
                await db.commit()

                # Store metrics
                bt_metrics = BacktestMetrics(
                    backtest_run_id=run.id,
                    total_trades=metrics.total_trades,
                    winning_trades=getattr(metrics, "winning_trades", 0),
                    losing_trades=getattr(metrics, "losing_trades", 0),
                    breakeven_trades=getattr(metrics, "breakeven_trades", 0),
                    effective_win_rate=getattr(metrics, "effective_win_rate", 0.0),
                    win_rate=metrics.win_rate,
                    net_profit=metrics.net_profit,
                    gross_profit=metrics.gross_profit,
                    gross_loss=metrics.gross_loss,
                    profit_factor=metrics.profit_factor,
                    max_drawdown=metrics.max_drawdown,
                    max_drawdown_pct=metrics.max_drawdown_pct,
                    sharpe_ratio=metrics.sharpe_ratio,
                    avg_rr=metrics.avg_rr,
                    equity_curve=metrics.equity_curve,
                    monthly_returns=metrics.monthly_returns,
                )
                db.add(bt_metrics)

                # Store individual trades
                for trade in runner.completed_trades:
                    bt_trade = BacktestTrade(
                        backtest_run_id=run.id,
                        instrument=trade.instrument,
                        direction=trade.direction,
                        entry_price=trade.entry_price,
                        exit_price=trade.exit_price or 0,
                        stop_loss=trade.stop_loss,
                        take_profit=trade.take_profit,
                        contracts=trade.contracts,
                        entry_time=trade.entry_time,
                        exit_time=trade.exit_time,
                        pnl=trade.pnl,
                        pnl_ticks=getattr(trade, "pnl_ticks", 0.0),
                        net_pnl=trade.net_pnl,
                        is_winner=trade.is_winner,
                        exit_reason=trade.exit_reason,
                    )
                    db.add(bt_trade)

                run.status = BacktestStatus.COMPLETED
                run.progress = 100.0
                run.completed_at = datetime.utcnow()
                await db.commit()
                logger.info(f"[backtest] run={run.id} COMPLETED")

            except Exception as e:
                import traceback
                _tb = traceback.format_exc()
                logger.error(f"[backtest] run={backtest_run_id} FAILED in engine/sim/metrics: "
                             f"{type(e).__name__}: {e}\n{_tb}")
                try:
                    await db.rollback()
                except Exception:
                    pass
                await _record_backtest_failure(backtest_run_id, f"{type(e).__name__}: {e}")

    except Exception as e:
        import traceback
        logger.error(f"[backtest] run={backtest_run_id} task crashed (outer): "
                     f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        await _record_backtest_failure(backtest_run_id, f"{type(e).__name__}: {e}")




@router.get("/compare")
async def compare_backtests(
    ids: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """COMPARE-BACKTESTS-V1: side-by-side diagnostic for 2+ runs.

    For each run: full config (snapshot incl. rule_tree/vwap/tp/engine + run-level
    fees/capital), metrics, data coverage (rows + first/last candle), and trade
    count by month. Plus `config_diff`: the exact keys that DIFFER across runs —
    which is what makes 'adjacent ranges, wildly different results' explainable.
    """
    run_ids = [x.strip() for x in (ids or "").split(",") if x.strip()][:6]
    if not run_ids:
        raise HTTPException(status_code=400, detail="Pass ?ids=runId1,runId2")
    import json as _json
    out = []
    for rid in run_ids:
        try:
            run = (await db.execute(select(BacktestRun).where(
                BacktestRun.id == rid, BacktestRun.user_id == current_user.id))).scalar_one_or_none()
        except Exception:
            run = None
        if not run:
            out.append({"id": rid, "error": "not found"}); continue
        m = (await db.execute(select(BacktestMetrics).where(
            BacktestMetrics.backtest_run_id == run.id))).scalar_one_or_none()
        snap = dict(run.strategy_params_snapshot or {})
        # run-level columns belong in the comparable config too
        snap.setdefault("initial_capital", run.initial_capital)
        snap.setdefault("commission_per_side", run.commission_per_side)
        snap.setdefault("slippage_ticks", run.slippage_ticks)
        try:
            rows = (await db.execute(text(
                "SELECT to_char(date_trunc('month', entry_time),'YYYY-MM') AS m, COUNT(*) AS n "
                "FROM backtest_trades WHERE backtest_run_id = :rid GROUP BY 1 ORDER BY 1"
            ), {"rid": str(run.id)})).fetchall()
            by_month = [{"month": r.m, "trades": int(r.n)} for r in rows]
        except Exception:
            by_month = []
        out.append({
            "id": str(run.id), "instrument": run.instrument,
            "start_date": run.start_date.isoformat(), "end_date": run.end_date.isoformat(),
            "timeframe": run.timeframe,
            "status": run.status.value if hasattr(run.status, "value") else str(run.status),
            "created_at": run.created_at.isoformat(),
            "config": snap,
            "data_coverage": {"rows": snap.get("data_rows"),
                              "first_ts": snap.get("data_first_ts"),
                              "last_ts": snap.get("data_last_ts")},
            "metrics": ({
                "total_trades": m.total_trades, "win_rate": m.win_rate,
                "profit_factor": m.profit_factor, "net_profit": m.net_profit,
                "max_drawdown_pct": m.max_drawdown_pct, "avg_rr": m.avg_rr,
                "sharpe_ratio": m.sharpe_ratio,
                "effective_win_rate": getattr(m, "effective_win_rate", None),
            } if m else None),
            "trade_count_by_month": by_month,
        })
    valid = [o for o in out if "config" in o]
    config_diff = []
    if len(valid) >= 2:
        allkeys = set()
        for o in valid:
            allkeys |= set((o["config"] or {}).keys())
        for k in sorted(allkeys):
            sig = [_json.dumps((o["config"] or {}).get(k), sort_keys=True, default=str) for o in valid]
            if len(set(sig)) > 1:
                config_diff.append({"key": k,
                                    "values": {o["id"]: (o["config"] or {}).get(k) for o in valid}})
    return {"runs": out, "config_diff": config_diff,
            "summary": (f"{len(config_diff)} config field(s) differ across the selected runs."
                        if config_diff else "Configs are identical across the selected runs.")}


@router.get("/{backtest_id}/progress")
async def get_backtest_progress(backtest_id: str):
    import redis as _redis
    _r = _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=False, db=0)
    pct = _r.get(f"backtest:{backtest_id}:progress")
    return {"progress": float(pct) if pct else 0.0}

# ── Backtest Trades & Chart Data ─────────────────────────────────────────────

class BacktestTradeResponse(BaseModel):
    id: str
    direction: str
    entry_price: float
    exit_price: float
    entry_time: str
    exit_time: str
    pnl: float
    net_pnl: float
    is_winner: bool
    exit_reason: str

@router.get("/{backtest_id}/trades", response_model=list[BacktestTradeResponse])
async def get_backtest_trades(
    backtest_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.backtest import BacktestTrade
    result = await db.execute(
        select(BacktestTrade).where(BacktestTrade.backtest_run_id == backtest_id)
        .order_by(BacktestTrade.entry_time)
    )
    trades = result.scalars().all()
    out = []
    for t in trades:
        out.append(BacktestTradeResponse(
            id=str(t.id), direction=t.direction.value if hasattr(t.direction, "value") else str(t.direction),
            entry_price=_safe_float(t.entry_price), exit_price=_safe_float(t.exit_price),
            entry_time=_iso_or_none(t.entry_time) or "", exit_time=_iso_or_none(t.exit_time) or "",
            pnl=_safe_float(t.pnl), net_pnl=_safe_float(t.net_pnl),
            is_winner=bool(t.is_winner), exit_reason=t.exit_reason or "",
        ))
    return out

@router.get("/{backtest_id}/chart-data")
async def get_backtest_chart_data(
    backtest_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return OHLCV data + trade markers for candlestick chart."""
    from app.models.backtest import BacktestRun, BacktestTrade
    # Get the backtest run
    run_result = await db.execute(
        select(BacktestRun).where(BacktestRun.id == backtest_id, BacktestRun.user_id == current_user.id)
    )
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Backtest not found")

    # Fetch price data
    from app.engines.backtest_engine.market_data_fetcher import fetch_futures_data
    df = await fetch_futures_data(run.instrument, run.start_date, run.end_date, run.timeframe or "15m")
    candles = []
    if df is not None and not df.empty:
        for ts, row in df.iterrows():
            candles.append({
                "time": int(ts.timestamp()),
                "open": round(float(row["open"]), 2),
                "high": round(float(row["high"]), 2),
                "low": round(float(row["low"]), 2),
                "close": round(float(row["close"]), 2),
            })

    # Get trades
    trades_result = await db.execute(
        select(BacktestTrade).where(BacktestTrade.backtest_run_id == backtest_id)
        .order_by(BacktestTrade.entry_time)
    )
    markers = []
    for t in trades_result.scalars().all():
        markers.append({
            "time": int(t.entry_time.timestamp()),
            "type": "entry",
            "direction": t.direction.value if hasattr(t.direction, "value") else str(t.direction),
            "price": t.entry_price,
            "is_winner": t.is_winner,
        })
        markers.append({
            "time": int(t.exit_time.timestamp()),
            "type": "exit",
            "direction": t.direction.value if hasattr(t.direction, "value") else str(t.direction),
            "price": t.exit_price or 0,
            "is_winner": t.is_winner,
        })

    return {"candles": candles, "markers": markers}


async def _run_options_backtest(run: "BacktestRun", strategy_model: "Strategy",
                                  db) -> None:
    """Route a run through OptionsBacktestEngine; write into the same
    backtest_trades and backtest_metrics tables so the existing UI works."""
    from app.engines.options.options_backtest import (
        OptionsBacktestEngine, OptionBacktestConfig, compute_options_metrics,
    )
    from app.engines.backtest_engine.ict_strategy import ICTStrategy
    from app.engines.strategy_engine.base_strategy import StrategyConfig

    run.status = BacktestStatus.RUNNING
    run.progress = 5.0
    await db.commit()

    underlying = (strategy_model.instruments or [run.instrument])[0]
    cfg = OptionBacktestConfig(
        underlying=underlying,
        start_date=run.start_date.date(),
        end_date=run.end_date.date(),
        starting_balance=run.initial_capital or 10_000.0,
        risk_per_trade_pct=float(getattr(strategy_model, "options_risk_per_trade_pct", 1.5) or 1.5),
        delta_min=float(getattr(strategy_model, "options_target_delta_min", 0.30) or 0.30),
        delta_max=float(getattr(strategy_model, "options_target_delta_max", 0.50) or 0.50),
        dte_min=int(getattr(strategy_model, "options_min_dte", 30) or 30),
        dte_max=int(getattr(strategy_model, "options_max_dte", 60) or 60),
        prefer_itm=bool(getattr(strategy_model, "options_prefer_itm", False)),
        spread_width=(int(getattr(strategy_model, "options_spread_width", 0) or 0)
                       if getattr(strategy_model, "options_mode", "") == "vertical_spread" else None),
        avoid_earnings_days=int(getattr(strategy_model, "options_avoid_earnings_days", 0) or 0),
        mode=str(getattr(strategy_model, "options_mode", "") or ""),
    )

    s_cfg = StrategyConfig(
        name=strategy_model.name, instruments=[underlying],
        primary_timeframe=strategy_model.primary_timeframe or "5m",
        execution_timeframe=strategy_model.execution_timeframe or "1m",
        higher_timeframes=strategy_model.higher_timeframes or ["1H"],
        risk_reward_ratio=strategy_model.risk_reward_ratio or 2.0,
        stop_loss_type=strategy_model.stop_loss_type or "structure",
        max_contracts=strategy_model.max_contracts or 1,
        fvg_min_size_ticks=strategy_model.fvg_min_size_ticks or 4,
    )
    strategy = ICTStrategy(s_cfg, instrument=underlying)

    import redis as _redis
    _r = _redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=False, db=0)
    def _progress_cb(pct):
        _r.set(f"backtest:{run.id}:progress", str(pct * 100), ex=3600)

    engine = OptionsBacktestEngine(cfg, strategy, progress_cb=_progress_cb)
    trades = await engine.run()

    run.progress = 90.0
    await db.commit()

    metrics = compute_options_metrics(trades, cfg.starting_balance)
    bt_metrics = BacktestMetrics(
        backtest_run_id=run.id,
        total_trades=metrics["total_trades"],
        winning_trades=metrics["winning_trades"],
        losing_trades=metrics["losing_trades"],
        breakeven_trades=metrics["breakeven_trades"],
        win_rate=metrics["win_rate"],
        effective_win_rate=metrics["effective_win_rate"],
        net_profit=metrics["net_profit"],
        gross_profit=metrics["gross_profit"],
        gross_loss=metrics["gross_loss"],
        profit_factor=metrics["profit_factor"],
        max_drawdown=metrics["max_drawdown"],
        max_drawdown_pct=metrics["max_drawdown_pct"],
        sharpe_ratio=metrics["sharpe_ratio"],
        avg_win=metrics["avg_win"],
        avg_loss=metrics["avg_loss"],
        avg_rr=metrics["avg_rr"],
        largest_win=metrics["largest_win"],
        largest_loss=metrics["largest_loss"],
        avg_trade_duration_minutes=metrics["avg_trade_duration_minutes"],
        equity_curve=metrics["equity_curve"],
        monthly_returns=metrics["monthly_returns"],
    )
    db.add(bt_metrics)

    for t in trades:
        bt_trade = BacktestTrade(
            backtest_run_id=run.id,
            instrument=t.contract_ticker,
            direction=t.direction,
            entry_price=t.entry_premium,
            exit_price=t.exit_premium,
            stop_loss=t.stop_premium,
            take_profit=t.target_premium,
            contracts=t.contracts,
            entry_time=t.entry_time,
            exit_time=t.exit_time,
            pnl=t.gross_pnl,
            pnl_ticks=0.0,
            commission=t.commission,
            slippage=0.0,
            net_pnl=t.net_pnl,
            is_winner=t.is_winner,
            exit_reason=t.exit_reason,
            conditions_snapshot={
                "strike": t.strike,
                "expiration": t.expiration.isoformat(),
                "right": t.right,
                "iv_used": t.iv_used,
                "entry_spot": t.entry_spot,
                "exit_spot": t.exit_spot,
                **t.metadata,
            },
        )
        db.add(bt_trade)

    run.status = BacktestStatus.COMPLETED
    run.progress = 100.0
    run.completed_at = datetime.utcnow()
    await db.commit()


@router.get("/ranking")
async def get_strategy_ranking(
    sort_by: str = "profit_factor",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bug 9: per-strategy ranking from each strategy's most recent COMPLETED
    backtest. Returns PF, win rate, drawdown, trade count, expectancy + a
    quality verdict (Bug 11) so the UI never presents a tiny-sample 95% as
    reliable. sort_by: profit_factor|win_rate|max_drawdown|total_trades|expectancy|net_profit."""
    rows = (await db.execute(_sql_text("""
        SELECT DISTINCT ON (s.id)
               s.id AS sid, s.name AS name, s.status::text AS status,
               m.profit_factor, m.win_rate, m.max_drawdown_pct,
               m.total_trades, m.net_profit
          FROM strategies s
          JOIN backtest_runs br ON br.strategy_id = s.id AND UPPER(br.status::text) = 'COMPLETED'
          JOIN backtest_metrics m ON m.backtest_run_id = br.id
         WHERE s.user_id = :uid
         ORDER BY s.id, br.completed_at DESC NULLS LAST
    """), {"uid": str(current_user.id)})).fetchall()

    out = []
    for r in rows:
        tt = int(r.total_trades or 0)
        expectancy = round((_safe_float(r.net_profit) / tt), 2) if tt > 0 else 0.0
        q = _assess_quality(_safe_float(r.profit_factor), _safe_float(r.win_rate),
                            _safe_float(r.max_drawdown_pct), tt, expectancy)
        out.append({
            "strategy_id": str(r.sid), "name": r.name, "status": (r.status or "").lower(),
            "profit_factor": round(_safe_float(r.profit_factor), 2),
            "win_rate": round(_safe_float(r.win_rate), 4),
            "max_drawdown_pct": round(_safe_float(r.max_drawdown_pct), 2),
            "total_trades": tt,
            "net_profit": round(_safe_float(r.net_profit), 2),
            "expectancy": expectancy,
            "quality": q,
        })

    key_map = {
        "profit_factor": "profit_factor", "win_rate": "win_rate",
        "max_drawdown": "max_drawdown_pct", "total_trades": "total_trades",
        "expectancy": "expectancy", "net_profit": "net_profit",
    }
    k = key_map.get(sort_by, "profit_factor")
    reverse = (k != "max_drawdown_pct")  # lower drawdown is better
    out.sort(key=lambda x: x.get(k, 0) or 0, reverse=reverse)
    for i, item in enumerate(out, 1):
        item["rank"] = i
    return out
