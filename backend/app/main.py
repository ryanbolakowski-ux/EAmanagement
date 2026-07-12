# Loguru: skip DEBUG to massively speed up backtests (was logging every bar)
import os as _os_log
import sys as _sys_log
from loguru import logger as _lg
_lg.remove()
_lg.add(_sys_log.stderr, level=_os_log.environ.get("LOG_LEVEL", "INFO"))

# Attach the in-memory ring-buffer sink so the Admin Systems Check endpoint
# can surface recent error log records without shelling out to docker logs.
# Wrapped in try/except: if the ring buffer module fails to import for any
# reason, the backend MUST still boot — the buffer is observability, not
# load-bearing.
try:
    from app.core.log_ring_buffer import install_ring_buffer_sink as _install_ring
    _install_ring()
except Exception as _ring_exc:
    _lg.warning(f"[ring-buffer] sink install failed: {_ring_exc}")

# Yfinance prep: just ensure the default ~/.cache/py-yfinance/ dir exists and
# is fresh on each container start. yfinance 1.3.0's set_tz_cache_location()
# has a regression that breaks subsequent Ticker() calls with TypeError, so
# we let it use its default location. The SQLite lock storms we hit earlier
# (5 watchers contending for tkr-tz.db) are now handled by the 60s result
# cache + global asyncio lock in account_signals/runner.py.
try:
    import pathlib as _pl
    _yf_cache = _pl.Path.home() / ".cache" / "py-yfinance"
    _yf_cache.mkdir(parents=True, exist_ok=True)
    # Wipe the stale SQLite file from last container run if present — fresh
    # state avoids any "WAL recovery" stalls on first Ticker() call.
    for f in _yf_cache.glob("tkr-tz.db*"):
        try: f.unlink()
        except: pass
except Exception:
    pass

import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger

from app.config import settings
from app.database import init_db
from app.api.routes import auth, strategies, backtests, trades, dashboard, optimization, paper_trading, live_trading, profile, options_paper, geo, kyc, scanner
from app.scripts.daily_data_fetch import run_daily_loop
from app.scripts.daily_digest import run_daily_digest_loop

# Task supervisor: crash-restart wrapper for the lifespan background loops
# (flag-gated via TASK_SUPERVISOR_ENABLED, default on). Guarded import: if it
# fails for any reason the backend MUST still boot — fall back to the exact
# old bare-create_task behavior.
try:
    from app.core.task_supervisor import supervise as _supervise
except Exception as _sup_exc:
    _lg.warning(f"[task-supervisor] import failed ({_sup_exc}); using bare create_task")
    def _supervise(factory, name, **_kw):
        return asyncio.create_task(factory())


_KYC_STARTUP_SYNC_RAN = False

async def _run_kyc_startup_sync() -> None:
    """Sweep every user with kyc_status='pending' AND a Stripe session id,
    pulling the authoritative status from Stripe. Per-user errors logged but
    do NOT block the rest. Runs ONCE per backend startup (module-level flag).
    """
    global _KYC_STARTUP_SYNC_RAN
    if _KYC_STARTUP_SYNC_RAN:
        logger.info("[kyc-startup-sync] already ran this process; skipping")
        return
    _KYC_STARTUP_SYNC_RAN = True

    try:
        from sqlalchemy import text as _t
        from app.database import async_session_factory as _asf
        from app.api.routes.kyc import sync_kyc_status_from_stripe
    except Exception as e:
        logger.warning(f"[kyc-startup-sync] import failed: {e}")
        return

    try:
        async with _asf() as _db:
            rows = (await _db.execute(_t(
                "SELECT id::text AS id, email, kyc_status, kyc_session_id "
                "FROM users "
                "WHERE kyc_status = 'pending' AND kyc_session_id IS NOT NULL"
            ))).mappings().all()
    except Exception as e:
        logger.warning(f"[kyc-startup-sync] could not fetch pending users: {e}")
        return

    logger.info(f"[kyc-startup-sync] sweeping {len(rows)} pending users")
    swept_changed = 0
    for r in rows:
        email = r.get("email") or "?"
        sid = r.get("kyc_session_id")
        before = r.get("kyc_status") or "?"
        try:
            async with _asf() as _db2:
                after = await sync_kyc_status_from_stripe(
                    _db2, user_id=str(r["id"]), session_id=sid
                )
            after_label = after or before
            if after and after != before:
                swept_changed += 1
            logger.info(
                f"[kyc-startup-sync] user={email} session={sid} "
                f"before={before} after={after_label}"
            )
        except Exception as e:
            logger.warning(
                f"[kyc-startup-sync] user={email} session={sid} error: {e}"
            )
    logger.info(
        f"[kyc-startup-sync] complete: swept={len(rows)} transitioned={swept_changed}"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Theta Algos API...")
    await init_db()
    logger.info("Database initialized.")

    # Auto-recover orphaned backtests + optimizations from previous run.
    # When the backend restarts mid-run, the worker process dies but the
    # DB row says RUNNING forever — the UI shows perpetual "40%". Mark
    # them FAILED on startup so the user can re-run cleanly.
    try:
        from app.database import async_session_factory as _asf_z
        from sqlalchemy import text as _t_z
        async with _asf_z() as _db_z:
            r1 = await _db_z.execute(_t_z(
                "UPDATE backtest_runs SET status='FAILED', completed_at=NOW(), "
                "error_message='Worker died during backend restart (auto-recovered on startup)' "
                "WHERE status IN ('RUNNING','PENDING') RETURNING id"
            ))
            n1 = len(r1.fetchall())
            # Optimizations are RESUMABLE — each distinct config is checkpointed to
            # optimization_runs.partial_results as it finishes. Instead of killing
            # them, flag RECOVERING and re-spawn the task; it replays the checkpoint
            # and runs only the unfinished configs.
            r2 = await _db_z.execute(_t_z(
                "UPDATE optimization_runs SET status='RECOVERING', "
                "error_message='Worker died during backend restart \u2014 auto-resuming from checkpoint' "
                "WHERE status IN ('RUNNING','RESUMED','RECOVERING','PENDING') "
                "AND (last_heartbeat_at IS NULL OR last_heartbeat_at < NOW() - INTERVAL '90 seconds') RETURNING id"
            ))
            _opt_ids = [str(row[0]) for row in r2.fetchall()]
            await _db_z.commit()
            if n1 or _opt_ids:
                logger.info(f"Startup recovery: marked {n1} stuck backtests FAILED; "
                            f"resuming {len(_opt_ids)} optimizations from checkpoint")
        for _oid in _opt_ids:
            try:
                import asyncio as _aio_o
                from app.api.routes.optimization import _run_optimization_wrapper as _orw
                _aio_o.create_task(_orw(_oid))
                logger.info(f"[OPT RECOVERY] re-spawned resume task for optimization {_oid}")
            except Exception as _re:
                logger.warning(f"opt recovery re-spawn failed for {_oid}: {_re}")
    except Exception as e:
        logger.warning(f"Startup zombie cleanup failed: {e}")

    # Resume active paper trading sessions
    try:
        from app.database import async_session_factory
        from sqlalchemy import select
        from app.models.trade import TradeSession, TradingMode
        from app.engines.paper_trading.runner import start_paper_session
        async with async_session_factory() as db:
            result = await db.execute(
                select(TradeSession).where(
                    TradeSession.is_active == True,
                    TradeSession.mode == TradingMode.PAPER,
                )
            )
            for sess in result.scalars().all():
                # Multi-instrument sessions store comma-separated instruments
                # (e.g. "ES,NQ"). Fan out one runner per instrument so the NQ
                # side of a multi-instrument session isn't silently dropped on
                # restart.
                instruments = [s.strip() for s in (sess.instrument or "ES").split(",") if s.strip()]
                for instrument in instruments:
                    logger.info(f"Resuming paper session: {sess.id} ({instrument})")
                    await start_paper_session(str(sess.id), str(sess.strategy_id), str(sess.user_id), instrument)
    except Exception as e:
        logger.warning("Failed to resume paper sessions: " + str(e))

    # Resume active account-signal watchers (Bug #2)
    # Without this, watchers only run after the POST that created them, so
    # every backend restart silently kills every running watcher and users
    # stop getting signal emails.
    try:
        from app.engines.account_signals.runner import start_watcher
        from app.database import async_session_factory as _asf
        from sqlalchemy import select as _select, text as _text
        async with _asf() as _db:
            _rows = await _db.execute(_text(
                "SELECT id, strategy_id, user_id, instruments, account_label, channels "
                "FROM account_signal_watchers WHERE is_active = true"
            ))
            for _r in _rows.fetchall():
                _instr = _r.instruments if isinstance(_r.instruments, list) else (_r.instruments or [])
                _chs = _r.channels if isinstance(_r.channels, list) else (_r.channels or ["email"])
                logger.info(f"Resuming signal watcher: {_r.id}")
                asyncio.create_task(start_watcher(
                    str(_r.id), str(_r.strategy_id), str(_r.user_id),
                    _instr, _r.account_label or "", _chs,
                ))
    except Exception as e:
        logger.warning(f"Failed to resume signal watchers: {e}")

    # Long-running lifespan loops are wrapped in the task supervisor: a crash
    # is logged + alerted + restarted with backoff instead of silently killing
    # the loop until the next deploy. Each _supervise(fn, name) is functionally
    # the old asyncio.create_task(fn()) plus supervision (and is EXACTLY that
    # when TASK_SUPERVISOR_ENABLED=0).
    from app.scripts.comp_expiry import run_comp_expiry_loop
    comp_expiry_task = _supervise(run_comp_expiry_loop, "comp_expiry_loop")
    daily_fetch_task = _supervise(run_daily_loop, "daily_data_fetch_loop")
    try:
        from app.engines.account_signals.runner import run_signal_resolution_loop as _rsrl
        signal_resolution_task = _supervise(_rsrl, "signal_resolution_loop")
        logger.info('[signals] scheduled resolution loop started (every 10m)')
    except Exception as _e:
        logger.warning(f'[signals] failed to start resolution loop: {_e}')
    digest_task = _supervise(run_daily_digest_loop, "daily_digest_loop")

    # Pre-market universe scanner — daily 08:30 ET signal scan + 08:45 auto-execute
    premarket_task = None
    try:
        from app.engines.options.premarket_scheduler import start_premarket_scheduler
        premarket_task = _supervise(start_premarket_scheduler, "premarket_scheduler")
    except Exception as e:
        logger.warning(f"Failed to start premarket scheduler: {e}")

    # Scanner health heartbeat — guarantees a 9:25 ET status email even on
    # quiet days so silent-failure of the morning pick can never recur.
    health_monitor_task = None
    try:
        from app.engines.scanner_health import run_health_monitor_loop
        health_monitor_task = _supervise(run_health_monitor_loop, "scanner_health_monitor")
    except Exception as e:
        logger.warning(f"Failed to start scanner health monitor: {e}")

    # Intraday candle_cache refresher — keeps ES/NQ/RTY/YM 1m bars fresh every
    # 60s during US market hours so the futures watchers never fall through to
    # rate-limited yfinance and go blind. Root cause of late/missing signal emails.
    intraday_refresh_task = None
    try:
        from app.scripts.intraday_data_refresh import run_intraday_refresh_loop
        intraday_refresh_task = _supervise(run_intraday_refresh_loop, "intraday_data_refresh")
    except Exception as e:
        logger.warning(f"Failed to start intraday data refresher: {e}")

    # Broker-balance sync — keeps broker_accounts.cached_* fresh every ~15 min
    # during market hours so the admin Systems Check broker_sync freshness check
    # is meaningful (before this, balances refreshed only on-demand).
    broker_balance_task = None
    try:
        from app.engines.live_trading.balance_sync import run_broker_balance_sync_loop
        broker_balance_task = _supervise(run_broker_balance_sync_loop, "broker_balance_sync")
    except Exception as e:
        logger.warning(f"Failed to start broker balance sync loop: {e}")

    # ── Real-time minute-bar ws feed (REALTIME-FEED-V1) ──
    # REALTIME_FEED=polygon streams AM.* minute aggregates into the in-process
    # LatestBarStore that the public tape, the futures signal proxy and the
    # Theta-scanner confirmation prefer over their delayed REST paths.
    # DEFAULT OFF: the current Polygon key has no ws entitlement yet — the
    # feed handles "not authorized" by retrying every ~15 min with a clear
    # log (no crash-loop), and every consumer degrades to today's behavior.
    # Go-live is one env flip + restart: docs/v2/11-realtime-feed-runbook.md.
    realtime_feed_task = None
    try:
        from app.engines.data_feeds.realtime_feed import create_feed_from_env
        _rt_feed = create_feed_from_env()  # None unless REALTIME_FEED is set
        if _rt_feed is not None:
            realtime_feed_task = _supervise(_rt_feed.start, "realtime_feed")
            logger.info("[realtime-feed] supervised ws feed task started")
    except Exception as e:
        logger.warning(f"Failed to start realtime feed: {e}")

    # ── KYC startup auto-sync (Stripe Identity webhook-loss safety net) ──
    # On every backend startup, pull the authoritative status from Stripe for
    # every user currently sitting at 'pending' with a Stripe session id.
    # Recovers users where Stripe verified them but we didn't see the webhook
    # (or our handler crashed before persisting). Idempotent — protected by a
    # module-level _KYC_STARTUP_SYNC_RAN flag so a hot-reload mid-startup never
    # double-runs. Per-user errors are isolated so one bad row can't block the
    # rest.
    try:
        await _run_kyc_startup_sync()
    except Exception as e:
        logger.warning(f"[kyc-startup-sync] outer guard caught: {e}")

    try:
        yield
    finally:
        daily_fetch_task.cancel()
        comp_expiry_task.cancel()
        digest_task.cancel()
        if premarket_task:
            premarket_task.cancel()
        if health_monitor_task:
            health_monitor_task.cancel()
        if intraday_refresh_task:
            intraday_refresh_task.cancel()
        if broker_balance_task:
            broker_balance_task.cancel()
        if realtime_feed_task:
            realtime_feed_task.cancel()
        logger.info("Shutting down Theta Algos API...")


# Disable interactive /docs + /redoc + /openapi.json in production. They
# advertise every endpoint + parameter shape, which is attacker reconnaissance
# for free. Set EXPOSE_DOCS=1 in dev/staging if you need them locally.
_expose_docs = _os_log.environ.get("EXPOSE_DOCS", "0") == "1"
app = FastAPI(
    title="Theta Algos API",
    description="Algorithmic futures trading platform — strategy building, backtesting, optimization, and live execution.",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _expose_docs else None,
    redoc_url="/redoc" if _expose_docs else None,
    openapi_url="/openapi.json" if _expose_docs else None,
)

from app.middleware.geo_block import geo_block_middleware
app.middleware("http")(geo_block_middleware)

# Defense-in-depth security headers on every response.
@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    # Strict-Transport-Security: nginx adds it on TLS endpoints, but cover here too
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

# Per-IP rate limiting on auth endpoints (slowapi). Catches brute-force
# password guessing + signup spam. Other endpoints unrestricted (they
# require a JWT, which is already an effective rate limiter).
try:
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    from slowapi.middleware import SlowAPIMiddleware
    from slowapi.errors import RateLimitExceeded
    from fastapi.responses import JSONResponse as _RL_JSON
    _limiter = Limiter(key_func=get_remote_address, default_limits=[])
    app.state.limiter = _limiter
    app.add_middleware(SlowAPIMiddleware)
    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_exceeded_handler(request, exc):
        return _RL_JSON(status_code=429, content={"detail": "Too many requests. Slow down."})
except ImportError:
    _limiter = None

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
from fastapi.responses import FileResponse
@app.get("/diag-login", include_in_schema=False)
async def diag_login_page():
    return FileResponse("/app/app/diag_login.html")

app.include_router(strategies.router, prefix="/api/v1/strategies", tags=["Strategies"])
app.include_router(backtests.router, prefix="/api/v1/backtests", tags=["Backtests"])
app.include_router(optimization.router, prefix="/api/v1/optimization", tags=["Optimization"])
app.include_router(options_paper.router, prefix="/api/v1/options-paper", tags=["options-paper"])
app.include_router(paper_trading.router, prefix="/api/v1/paper-trading", tags=["Paper Trading"])
app.include_router(live_trading.router, prefix="/api/v1/live-trading", tags=["Live Trading"])
app.include_router(trades.router, prefix="/api/v1/trades", tags=["Trades"])
app.include_router(dashboard.router, prefix="/api/v1/dashboard", tags=["Dashboard"])
app.include_router(geo.router, prefix="/api/v1/geo", tags=["geo"])
app.include_router(kyc.router, prefix="/api/v1/kyc", tags=["kyc"])
app.include_router(profile.router, prefix="/api/v1/profile", tags=["Profile"])
from app.api.routes import stripe_billing
from app.api.routes import admin as admin_routes
from app.api.routes import account_signals
from app.api.routes import legal
from app.api.routes import options as options_routes
from app.api.routes import security
from app.api.routes import support as support_routes
app.include_router(stripe_billing.router, prefix="/api/v1/billing", tags=["billing"])
app.include_router(admin_routes.router, prefix="/api/v1/admin", tags=["admin"])
app.include_router(legal.router, prefix="/api/v1/legal", tags=["legal"])
app.include_router(security.router, prefix="/api/v1/security", tags=["Security"])
app.include_router(options_routes.router, prefix="/api/v1/options", tags=["options"])
app.include_router(support_routes.router, prefix="/api/v1/support", tags=["support"])
app.include_router(account_signals.router, prefix="/api/v1/account-signals", tags=["account-signals"])
# Alias: the UI brands this feature "Email Signals". Mount the SAME router
# under /api/v1/email-signals so either path resolves (no hidden 404s).
app.include_router(account_signals.router, prefix="/api/v1/email-signals", tags=["account-signals"])
app.include_router(scanner.router, prefix="/api/v1/scanner", tags=["scanner"])

# iOS companion app: APNs device-token registry (push notifications).
# Sends are hard-gated by APNS_ENABLED (default off) in app/services/push.py.
from app.api.routes import devices as devices_routes
app.include_router(devices_routes.router, prefix="/api/v1/devices", tags=["devices"])

# V2 dashboard live updates over Server-Sent Events (ENABLE_SSE_DASHBOARD,
# default on; flag off => the route 404s and the frontend falls back to
# polling). See app/api/routes/stream.py for the SSE-vs-WebSocket rationale.
from app.api.routes import stream as stream_routes
app.include_router(stream_routes.router, prefix="/api/v1/stream", tags=["stream"])

# Public landing-page tape: NO auth (the marketing page is unauthenticated).
# Fixed server-side symbol list + 60s TTL cache; can never 500 by design.
from app.api.routes import public_tape
app.include_router(public_tape.router, prefix="/api/v1/public", tags=["public"])


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": "edge-asset-management"}


@app.get("/health/full", tags=["Health"])
async def health_full():
    """Dependency health for monitoring/alerting: database, queue (redis),
    email provider, auth token round-trip, and worker liveness (no runs stuck
    RUNNING > 30 min). 200 when all critical deps are up, else 503."""
    from fastapi.responses import JSONResponse
    components: dict = {}

    # Reuse the scanner health checks (db / redis / resend / polygon).
    try:
        from app.engines.scanner_health import check_health as _ch
        base = await _ch()
        components.update(base.get("components", {}))
    except Exception as e:
        components["scanner_health"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    # Auth: mint + decode a token round-trip (no user creds involved).
    try:
        from app.core.security import create_access_token, decode_token
        _t = create_access_token({"sub": "healthcheck"})
        components["auth"] = {"ok": decode_token(_t).get("sub") == "healthcheck"}
    except Exception as e:
        components["auth"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    # Worker: nothing stuck RUNNING for more than 30 minutes.
    try:
        from app.database import async_session_factory as _asf
        from sqlalchemy import text as _t2
        async with _asf() as _db:
            stuck = (await _db.execute(_t2(
                "SELECT count(*) FROM optimization_runs "
                "WHERE status='RUNNING' AND started_at < NOW() - INTERVAL '30 minutes'"
            ))).scalar()
        components["worker"] = {"ok": (stuck or 0) == 0, "stuck_runs": int(stuck or 0)}
    except Exception as e:
        components["worker"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}

    # Alpaca IEX real-time feed (PREFERRED futures source). Free tier, penny-
    # accurate for SPY/QQQ. configured = both env keys present; reachable = a
    # test SPY bar fetch succeeded. Never exposes the secret; never fatal.
    try:
        import os as _os_a
        configured = bool(_os_a.environ.get("ALPACA_API_KEY") and _os_a.environ.get("ALPACA_API_SECRET"))
        reachable = False
        if configured:
            try:
                from app.engines.data_feeds.alpaca_feed import fetch_alpaca_bars
                _df = fetch_alpaca_bars("SPY", timeframe="1Min", limit=1)
                reachable = _df is not None and not _df.empty
            except Exception:
                reachable = False
        components["alpaca"] = {"configured": configured, "feed": "iex", "reachable": reachable}
    except Exception as e:
        components["alpaca"] = {"configured": False, "feed": "iex", "reachable": False,
                                "error": f"{type(e).__name__}: {str(e)[:120]}"}

    # Critical deps that gate overall health (email/polygon flakiness is not fatal).
    critical = ["database", "redis", "auth", "worker"]
    ok = all(components.get(k, {}).get("ok", False) for k in critical if k in components)
    body = {"status": "ok" if ok else "degraded", "components": components}
    return JSONResponse(body, status_code=200 if ok else 503)

