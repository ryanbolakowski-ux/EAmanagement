from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from loguru import logger

from app.config import settings
from app.database import init_db
from app.api.routes import auth, strategies, backtests, trades, dashboard, optimization, paper_trading, live_trading


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Edge Asset Management API...")
    await init_db()
    logger.info("Database initialized.")
    yield
    logger.info("Shutting down Edge Asset Management API...")


app = FastAPI(
    title="Edge Asset Management API",
    description="Algorithmic futures trading platform — strategy building, backtesting, optimization, and live execution.",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
app.include_router(strategies.router, prefix="/api/v1/strategies", tags=["Strategies"])
app.include_router(backtests.router, prefix="/api/v1/backtests", tags=["Backtests"])
app.include_router(optimization.router, prefix="/api/v1/optimization", tags=["Optimization"])
app.include_router(paper_trading.router, prefix="/api/v1/paper-trading", tags=["Paper Trading"])
app.include_router(live_trading.router, prefix="/api/v1/live-trading", tags=["Live Trading"])
app.include_router(trades.router, prefix="/api/v1/trades", tags=["Trades"])
app.include_router(dashboard.router, prefix="/api/v1/dashboard", tags=["Dashboard"])


@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "ok", "service": "edge-asset-management"}
