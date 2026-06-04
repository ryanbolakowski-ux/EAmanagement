from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text

from app.database import get_db
from app.models.user import User
from app.models.trade import Trade, TradingMode, TradeStatus
from app.models.strategy import Strategy
from app.models.backtest import BacktestRun
from app.core.auth import require_2fa_when_paid as get_current_user
from app.engines.ict_bias import compute_ict_bias

router = APIRouter()
# 2FA gate: routes here require totp_enabled if user is on paid/trial subscription

DAILY_BIAS_INSTRUMENTS = ["ES", "NQ", "RTY", "YM"]


async def _compute_daily_bias(db: AsyncSession, instrument: str) -> dict:
    """Compute today's ICT-style directional bias for an instrument.

    Pulls 60 days of 1m bars from candle_cache and runs the ICT bias engine,
    which combines the 30-day EMA trend (as context) with intraday structure:
    PDH/PDL position, opening type, Asian-range sweeps, draw on liquidity.
    See `app/engines/ict_bias.py` for the full rule set.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=60)

    rows = (await db.execute(
        text("""
            SELECT timestamp, open, high, low, close, volume FROM candle_cache
            WHERE instrument = :inst AND timestamp >= :start
            ORDER BY timestamp
        """),
        {"inst": instrument, "start": start},
    )).all()

    # Engine consumes a list of (ts, o, h, l, c, v) tuples
    return compute_ict_bias([tuple(r) for r in rows], instrument)


@router.get("/bias")
async def get_daily_bias(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    biases = [await _compute_daily_bias(db, inst) for inst in DAILY_BIAS_INSTRUMENTS]
    return {"biases": biases}


async def _bias_detail(db: AsyncSession, instrument: str) -> dict:
    """Detailed bias breakdown for one instrument: daily candles + EMAs +
    higher-timeframe (1H) Fair Value Gap analysis with respect/disrespect tags.
    """
    from app.engines.strategy_engine.indicators import detect_fvgs

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=60)

    rows = (await db.execute(
        text("""
            SELECT timestamp, open, high, low, close, volume FROM candle_cache
            WHERE instrument = :inst AND timestamp >= :start
            ORDER BY timestamp
        """),
        {"inst": instrument, "start": start},
    )).all()

    empty = {
        "instrument": instrument, "bias": "neutral", "strength_pct": 0.0,
        "last_close": None, "ema_fast": None, "ema_slow": None,
        "candles": [], "ema_fast_series": [], "ema_slow_series": [],
        "htf_fvgs": [], "summary": "Not enough data to compute bias yet.",
    }

    if len(rows) < 60:
        return empty

    # ICT bias headline (uses 1m bars + intraday ICT structure)
    ict = compute_ict_bias([tuple(r) for r in rows], instrument)
    bias        = ict["bias"]
    spread_pct  = ict["trend_strength_pct"]
    last_close  = ict["last_close"]
    fast_now    = ict["ema_fast"]
    slow_now    = ict["ema_slow"]

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()

    # Daily aggregation for the bias chart (kept verbatim — the chart needs
    # daily candles + EMA series even though the headline uses richer logic)
    daily = df.resample("1D").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna(subset=["close"])
    if len(daily) < 21:
        return empty

    fast = daily["close"].ewm(span=9, adjust=False).mean()
    slow = daily["close"].ewm(span=21, adjust=False).mean()

    # 1H FVG detection — last ~14 days
    hourly = df.resample("1h").agg({
        "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum",
    }).dropna(subset=["close"]).tail(14 * 24)
    fvgs = []
    if len(hourly) >= 3:
        fvgs = detect_fvgs(hourly, instrument=instrument, min_size_ticks=4.0)

    htf_fvgs = []
    bull_respect = bull_disrespect = 0
    bear_respect = bear_disrespect = 0
    for fvg in fvgs[-30:]:  # cap response payload
        # An unfilled bullish FVG below the current price = respected (price held above)
        # An unfilled bullish FVG above current price = price collapsed through it (disrespected)
        # Mirror for bearish.
        if fvg.direction == "bullish":
            respected = (not fvg.filled) and (last_close >= fvg.low)
        else:
            respected = (not fvg.filled) and (last_close <= fvg.high)

        if fvg.direction == "bullish":
            if respected: bull_respect += 1
            else: bull_disrespect += 1
        else:
            if respected: bear_respect += 1
            else: bear_disrespect += 1

        htf_fvgs.append({
            "direction": fvg.direction,
            "high": round(fvg.high, 2),
            "low": round(fvg.low, 2),
            "ce": round(fvg.ce_level, 2),
            "size_ticks": round(fvg.size_ticks, 1),
            "filled": bool(fvg.filled),
            "timestamp": fvg.timestamp.isoformat() if hasattr(fvg.timestamp, "isoformat") else str(fvg.timestamp),
            "respected": bool(respected),
        })

    # Plain-English summary — starts with the ICT narrative, then adds
    # 1H FVG respect/disrespect context for the chart panel.
    parts = [ict["narrative"]]
    if bull_respect or bull_disrespect:
        parts.append(f"On the 1H, price is respecting {bull_respect} bullish FVGs and disrespecting {bull_disrespect}.")
    if bear_respect or bear_disrespect:
        parts.append(f"There are {bear_respect} bearish FVGs holding overhead and {bear_disrespect} that have been broken through.")
    if bias.endswith("bullish") and bull_respect > bull_disrespect:
        parts.append("HTF gaps confirm the bullish bias.")
    elif bias.endswith("bearish") and bear_respect > bear_disrespect:
        parts.append("HTF gaps confirm the bearish bias.")
    elif bias != "neutral":
        parts.append("HTF gaps are not yet confirming the bias — be cautious.")

    candles_payload = [
        {
            "time": int(ts.timestamp()),
            "open": round(float(row.open), 2),
            "high": round(float(row.high), 2),
            "low": round(float(row.low), 2),
            "close": round(float(row.close), 2),
        }
        for ts, row in daily.iterrows()
    ]

    return {
        "instrument": instrument,
        "bias": bias,
        "strength_pct": round(spread_pct, 2),
        "last_close": round(last_close, 2) if last_close is not None else None,
        "ema_fast": round(fast_now, 2) if fast_now is not None else None,
        "ema_slow": round(slow_now, 2) if slow_now is not None else None,
        "candles": candles_payload,
        "ema_fast_series": [round(float(v), 2) for v in fast.values],
        "ema_slow_series": [round(float(v), 2) for v in slow.values],
        "htf_fvgs": htf_fvgs,
        "summary": " ".join(parts),
        # ── ICT enrichment ─────────────────────────────────────────
        "trend":               ict["trend"],
        "trend_strength_pct":  ict["trend_strength_pct"],
        "pdh":                 ict["pdh"],
        "pdl":                 ict["pdl"],
        "pdc":                 ict["pdc"],
        "position_vs_pd":      ict["position_vs_pd"],
        "opening_type":        ict["opening_type"],
        "asian_high":          ict["asian_high"],
        "asian_low":           ict["asian_low"],
        "pdh_swept":           ict["pdh_swept"],
        "pdl_swept":           ict["pdl_swept"],
        "asian_swept_high":    ict["asian_swept_high"],
        "asian_swept_low":     ict["asian_swept_low"],
        "current_session":     ict["current_session"],
        "draw_target":         ict["draw_target"],
        "narrative":           ict["narrative"],
    }


@router.get("/bias/detail")
async def get_bias_detail(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    out = []
    for inst in DAILY_BIAS_INSTRUMENTS:
        out.append(await _bias_detail(db, inst))
    return {"instruments": out}


@router.get("/summary")
async def get_dashboard_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Strategy count
    strat_count = (await db.execute(
        select(func.count()).where(Strategy.user_id == current_user.id)
    )).scalar()

    # Backtest count
    bt_count = (await db.execute(
        select(func.count()).where(BacktestRun.user_id == current_user.id)
    )).scalar()

    # Paper trading stats
    paper_trades = (await db.execute(
        select(Trade).where(
            Trade.user_id == current_user.id,
            Trade.mode == TradingMode.PAPER,
            Trade.status == TradeStatus.CLOSED,
        )
    )).scalars().all()

    paper_pnl   = sum(t.net_pnl or 0 for t in paper_trades)
    paper_wins  = sum(1 for t in paper_trades if (t.net_pnl or 0) > 0)
    paper_wr    = (paper_wins / len(paper_trades)) if paper_trades else 0.0

    # Live trading stats
    live_trades = (await db.execute(
        select(Trade).where(
            Trade.user_id == current_user.id,
            Trade.mode == TradingMode.LIVE,
            Trade.status == TradeStatus.CLOSED,
        )
    )).scalars().all()

    live_pnl  = sum(t.net_pnl or 0 for t in live_trades)
    live_wins = sum(1 for t in live_trades if (t.net_pnl or 0) > 0)
    live_wr   = (live_wins / len(live_trades)) if live_trades else 0.0

    return {
        "strategy_count": strat_count,
        "backtest_count":  bt_count,
        "subscription_tier": current_user.subscription_tier,
        "paper_trading": {
            "total_trades": len(paper_trades),
            "net_pnl":      round(paper_pnl, 2),
            "win_rate":     round(paper_wr, 4),
        },
        "live_trading": {
            "total_trades": len(live_trades),
            "net_pnl":      round(live_pnl, 2),
            "win_rate":     round(live_wr, 4),
        },
    }


@router.get("/market-status")
async def get_market_status():
    """Returns whether US equity markets are open right now, plus
    holiday info. Used by the dashboard 'Today’s Pick' card to
    explain why no pick was generated on closed days."""
    from app.engines.market_calendar import market_status
    return market_status()

