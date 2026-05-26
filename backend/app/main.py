# Loguru: skip DEBUG to massively speed up backtests (was logging every bar)
import os as _os_log
import sys as _sys_log
from loguru import logger as _lg
_lg.remove()
_lg.add(_sys_log.stderr, level=_os_log.environ.get("LOG_LEVEL", "INFO"))

# Disable yfinance's SQLite tz-cache. Every yf.Ticker() opens a connection
# to /root/.cache/py-yfinance/tkr-tz.db; with 5+ watchers and a backtest
# running concurrently, the SQLite WAL contention pegs every Python thread
# in S(sleeping) state and the backtest hangs at whatever % it was at.
try:
    import yfinance as _yf_init
    try:
        _yf_init.set_tz_cache_location(None)
    except Exception:
        import tempfile as _yf_tmp
        _os_log.environ['YF_CACHE_DIR'] = _yf_tmp.mkdtemp(prefix='yf-')
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

    try:
        yield
    finally:
        daily_fetch_task.cancel()
        comp_expiry_task.cancel()
        digest_task.cancel()
        if premarket_task:
            premarket_task.cancel()
        logger.info("Shutting down Theta Algos API...")


app = FastAPI(
    title="Theta Algos API",
    description="Algorithmic futures trading platform — strategy building, backtesting, optimization, and live execution.",
    version="0.1.0",
    lifespan=lifespan,
)

from app.middleware.geo_block import geo_block_middleware
app.middleware("http")(geo_block_middleware)

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

