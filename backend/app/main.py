# Loguru: skip DEBUG to massively speed up backtests (was logging every bar)
import os as _os_log
import sys as _sys_log
from loguru import logger as _lg
_lg.remove()
_lg.add(_sys_log.stderr, level=_os_log.environ.get("LOG_LEVEL", "INFO"))

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
                "UPDATE backtest_runs SET status='FAILED', "
                "error_message='Worker died during backend restart (auto-recovered on startup)' "
                "WHERE status IN ('RUNNING','PENDING') RETURNING id"
            ))
            n1 = len(r1.fetchall())
            r2 = await _db_z.execute(_t_z(
                "UPDATE optimization_runs SET status='FAILED', "
                "error_message='Worker died during backend restart (auto-recovered on startup)' "
                "WHERE status IN ('RUNNING','PENDING') RETURNING id"
            ))
            n2 = len(r2.fetchall())
            await _db_z.commit()
            if n1 or n2:
                logger.info(f"Startup recovery: marked {n1} stuck backtests + {n2} stuck optimizations FAILED")
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

    from app.scripts.comp_expiry import run_comp_expiry_loop
    comp_expiry_task = asyncio.create_task(run_comp_expiry_loop())
    daily_fetch_task = asyncio.create_task(run_daily_loop())
    digest_task = asyncio.create_task(run_daily_digest_loop())

    # Pre-market universe scanner — daily 08:30 ET signal scan + 08:45 auto-execute
    premarket_task = None
    try:
        from app.engines.options.premarket_scheduler import start_premarket_scheduler
        premarket_task = asyncio.create_task(start_premarket_scheduler())
    except Exception as e:
        logger.warning(f"Failed to start premarket scheduler: {e}")

    # Scanner health heartbeat — guarantees a 9:25 ET status email even on
    # quiet days so silent-failure of the morning pick can never recur.
    health_monitor_task = None
    try:
        from app.engines.scanner_health import run_health_monitor_loop
        health_monitor_task = asyncio.create_task(run_health_monitor_loop())
    except Exception as e:
        logger.warning(f"Failed to start scanner health monitor: {e}")

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
from app.api.routes import support as support_routes
app.include_router(stripe_billing.router, prefix="/api/v1/billing", tags=["billing"])
app.include_router(admin_routes.router, prefix="/api/v1/admin", tags=["admin"])
app.include_router(legal.router, prefix="/api/v1/legal", tags=["legal"])
app.include_router(options_routes.router, prefix="/api/v1/options", tags=["options"])
app.include_router(support_routes.router, prefix="/api/v1/support", tags=["support"])
app.include_router(account_signals.router, prefix="/api/v1/account-signals", tags=["account-signals"])
app.include_router(scanner.router, prefix="/api/v1/scanner", tags=["scanner"])


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": "edge-asset-management"}

